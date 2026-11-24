"""Reconciler and outbox. Every function is safe to run from any worker at any time, any number of
times: each one finds work with a query and acts with a compare-and-swap, so overlapping runs
either do the same idempotent thing or one of them changes nothing.
"""
from typing import Dict

from . import store
from .calls import CallService, NON_TERMINAL
from .clock import Clock
from .config import CampaignConfig
from .db import Database, bump
from .models import ProviderError, ProviderEvent
from .providers.router import ProviderRouter
from .states import CallState as C


class Reconciler:
    def __init__(self, db: Database, clock: Clock, calls: CallService, router: ProviderRouter,
                 campaigns: Dict[str, CampaignConfig], worker_id: str):
        self.db, self.clock, self.calls, self.router, self.campaigns, self.worker = db, clock, calls, router, campaigns, worker_id
        self.counters = dict(adopted=0, released=0, force_failed=0, orphans_repaired=0, commands_sent=0, commands_failed=0)

    # ------------------------------------------------------------------ stuck calls
    def run(self, campaign_id: str):
        t = self.campaigns[campaign_id].timing
        now = self.clock.now()
        conn = self.db.conn()
        stuck = conn.execute(
            "SELECT * FROM calls WHERE campaign_id=? AND ("
            " (state='RESERVED' AND state_since<=?)"
            " OR (state IN ('INITIATED','RINGING') AND state_since<=? AND COALESCE(checked_at,0)<=?)"
            " OR (state='CONNECTED' AND connected_at<=? AND COALESCE(checked_at,0)<=?)) LIMIT 200",
            (campaign_id, now - t.reserve_lease_s, now - t.ring_deadline_s, now - t.recheck_s,
             now - t.connected_check_s, now - t.recheck_s)).fetchall()
        for call in stuck:
            self._reconcile(call, t, now)
        self._repair_orphans(campaign_id, now)

    def _event(self, call_id, provider, typ, talk=0.0):
        return ProviderEvent(provider, f"recon:{call_id}:{typ}", call_id, typ, self.clock.now(), talk)

    def _reconcile(self, call, t, now):
        cid, state = call["id"], call["state"]
        with self.db.tx() as conn:                       # claim the check so two workers don't both do it
            cur = conn.execute("UPDATE calls SET checked_at=? WHERE id=? AND state=? AND COALESCE(checked_at,0)<=?",
                               (now, cid, state, now - t.recheck_s + 0.001))
            if cur.rowcount != 1:
                return
        try:
            found = self.router.find_call(cid) if not call["provider"] else (call["provider"], self.router.status(call["provider"], cid))
        except ProviderError:
            if call["provider"]:
                self.router.record_failure(call["provider"])
            age = now - call["state_since"]
            if state != "CONNECTED" and age > 2 * t.ring_deadline_s:
                self._give_up(call, "provider_unreachable", now)
            elif state == "CONNECTED" and now - call["connected_at"] > t.max_call_s:
                self._force_close(call, now)
            return
        if found is None or not found[1].known:
            if state == "RESERVED":
                self.counters["released"] += 1
                self.calls.fail_placement(cid, "recovered_unplaced", None, ambiguous=False)
            else:
                self._give_up(call, "provider_lost_call", now)
            return
        prov, st = found
        if not call["provider"]:
            with self.db.tx() as conn:
                conn.execute("UPDATE calls SET provider=? WHERE id=? AND provider IS NULL", (prov, cid))
        self.counters["adopted"] += 1
        if st.state == "ENDED":
            if st.talk_s > 0:
                self.calls.handle_event(self._event(cid, prov, "COMPLETED", st.talk_s))
            else:
                self.calls.handle_event(self._event(cid, prov, "NO_ANSWER"))
        elif st.state == "ANSWERED":
            self.calls.handle_event(self._event(cid, prov, "ANSWERED"))
            if state == "CONNECTED" and now - call["connected_at"] > t.max_call_s:
                self._force_close(call, now)
        elif st.state == "RINGING":
            self.calls.handle_event(self._event(cid, prov, "RINGING"))
            if now - call["state_since"] > t.ring_deadline_s * 2:
                self.calls.cancel_call(cid, "ring_too_long")

    def _give_up(self, call, reason, now):
        self.counters["force_failed"] += 1
        with self.db.tx() as conn:
            self.calls._end(conn, call["id"], C.FAILED, now, outcome="PROVIDER_LOST", reason=reason,
                            borrower_outcome="SYSTEM", cause="reconcile_" + reason, cmd="CANCEL", provider=call["provider"] or self._any_provider())
            bump(conn, call["campaign_id"], now, sys_fail=1)

    def _force_close(self, call, now):
        self.counters["force_failed"] += 1
        with self.db.tx() as conn:
            self.calls._end(conn, call["id"], C.COMPLETED, now, outcome="FORCED_CLOSE", reason="max_call_duration",
                            borrower_outcome="DONE", cause="reconcile_max_call", wrap=True, cmd="HANGUP")

    def _any_provider(self):
        return next(iter(self.router.providers))

    # ------------------------------------------------------------------ invariants
    def _repair_orphans(self, campaign_id, now):
        """Should never find anything (reservations and call rows are written in one transaction).
        It exists so that if some future bug or manual edit breaks that, the system heals and says so."""
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT a.id, a.call_id FROM agents a LEFT JOIN calls c ON c.id=a.call_id WHERE a.campaign_id=? AND "
            "a.state IN ('RESERVED','DIALING','CONNECTED') AND (c.id IS NULL OR c.state IN ('COMPLETED','FAILED','CANCELLED','ABANDONED'))",
            (campaign_id,)).fetchall()
        brs = conn.execute(
            "SELECT b.id, b.claimed_by_call FROM borrowers b LEFT JOIN calls c ON c.id=b.claimed_by_call WHERE b.campaign_id=? AND "
            "b.status='CLAIMED' AND b.claim_until<=? AND (c.id IS NULL OR c.state IN ('COMPLETED','FAILED','CANCELLED','ABANDONED'))",
            (campaign_id, now)).fetchall()
        if not rows and not brs:
            return
        with self.db.tx() as c2:
            for r in rows:
                if store.release_agent(c2, now, r["id"], r["call_id"], worker=self.worker, cause="orphan_repair"):
                    self.counters["orphans_repaired"] += 1
            for b in brs:
                c2.execute("UPDATE borrowers SET status='PENDING', claimed_by_call=NULL, claim_until=NULL WHERE id=? AND claimed_by_call=?",
                           (b["id"], b["claimed_by_call"]))
                self.counters["orphans_repaired"] += 1
            self.calls.serve_holds(c2, campaign_id, now)

    # ------------------------------------------------------------------ outbox
    def send_commands(self, limit: int = 100):
        now = self.clock.now()
        conn = self.db.conn()
        rows = conn.execute("SELECT * FROM provider_commands WHERE status='PENDING' AND next_at<=? ORDER BY id LIMIT ?",
                            (now, limit)).fetchall()
        for r in rows:
            with self.db.tx() as c2:                     # lease the command to this worker for 10s
                cur = c2.execute("UPDATE provider_commands SET next_at=? WHERE id=? AND status='PENDING' AND next_at<=?",
                                 (now + 10.0, r["id"], now))
                if cur.rowcount != 1:
                    continue
            try:
                self.router.cancel(r["provider"], r["call_id"])
                status, nxt = "DONE", now
                self.counters["commands_sent"] += 1
            except ProviderError:
                att = r["attempts"] + 1
                status = "PENDING" if att < 8 else "GAVE_UP"
                nxt = now + min(30.0, 2.0 ** att)
                self.counters["commands_failed"] += 1
                self.router.record_failure(r["provider"])
            with self.db.tx() as c2:
                c2.execute("UPDATE provider_commands SET status=?, attempts=attempts+1, next_at=? WHERE id=?",
                           (status, nxt, r["id"]))
