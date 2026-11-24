"""Call lifecycle: the only place where call and agent states change in response to the outside world.

Principles
* Every provider event is processed in ONE transaction: dedupe row + call transition + agent
  release + borrower update + metrics. Either all of it happened or none of it did. A worker that
  crashes mid-event leaves nothing half-done; redelivery (or the reconciler) finishes the job.
* Dedupe has two layers. (provider, event_id) is a primary key, which kills exact redelivery.
  Then the transition itself is forward-only by rank, so a *different* event id saying the same
  thing (ANSWERED, ANSWERED, ANSWERED) is a no-op as well.
* Terminal states are final. Late events for a finished call are recorded and ignored, with one
  exception: ANSWERED for a call we already gave up on means a live call we don't track, so we
  queue a hangup.
* Anything that must be told to the provider (cancel / hangup) goes into an outbox table in the
  same transaction as the state change, and is sent afterwards with retries.
"""
from typing import Dict, Optional

from . import store
from .config import CampaignConfig
from .db import Database, bump
from .clock import Clock
from .models import ProviderEvent
from .states import AgentState as A, CallState as C, CALL_TERMINAL, CALL_RANK, CALL_UNANSWERED

NON_TERMINAL = tuple(s.value for s in C if s not in CALL_TERMINAL)
UNANSWERED = tuple(s.value for s in CALL_UNANSWERED)


class CallService:
    def __init__(self, db: Database, clock: Clock, worker_id: str, campaigns: Dict[str, CampaignConfig],
                 hold_grace_s: float = 2.0):
        self.db, self.clock, self.worker, self.campaigns, self.hold_grace_s = db, clock, worker_id, campaigns, hold_grace_s

    # ------------------------------------------------------------------ helpers
    def _cfg(self, campaign_id) -> CampaignConfig:
        return self.campaigns[campaign_id]

    def _enqueue(self, conn, call, cmd, now, provider: Optional[str] = None):
        prov = provider or call["provider"]
        if prov:
            conn.execute("INSERT OR IGNORE INTO provider_commands(call_id,provider,cmd,next_at) VALUES(?,?,?,?)",
                         (call["id"], prov, cmd, now))

    def _borrower(self, conn, call, now, outcome):
        r = self._cfg(call["campaign_id"]).retry
        store.borrower_after(conn, call["borrower_id"], call["id"], now, outcome=outcome, max_attempts=r.max_attempts,
                             backoff_s=r.backoff_s, system_retry_s=r.system_retry_s,
                             max_system_failures=r.max_system_failures, abandoned_callback_s=r.abandoned_callback_s)

    def _end(self, conn, call_id, new_state: C, now, *, outcome, reason, borrower_outcome, cause,
             wrap: bool = False, cmd: Optional[str] = None, provider: Optional[str] = None) -> bool:
        """Move a non-terminal call to a terminal state and settle everything that hung off it."""
        call = store.get_call(conn, call_id)
        if call is None or call["state"] not in NON_TERMINAL:
            return False
        if not store.set_call(conn, call_id, NON_TERMINAL, new_state.value, now, cause, self.worker,
                              ended_at=now, outcome=outcome, reason=reason):
            return False
        if call["agent_id"]:
            wrap_s = self._cfg(call["campaign_id"]).timing.wrap_up_s if wrap else None
            new = store.release_agent(conn, now, call["agent_id"], call_id, wrap_s=wrap_s, worker=self.worker, cause=cause)
            if new == A.AVAILABLE.value:
                self.serve_holds(conn, call["campaign_id"], now)
        self._borrower(conn, call, now, borrower_outcome)
        if cmd:
            self._enqueue(conn, call, cmd, now, provider)
        return True

    def _agent_dialing(self, conn, call, now):
        if call["agent_id"]:
            cur = conn.execute("UPDATE agents SET state='DIALING', version=version+1, state_since=?, reserved_until=NULL "
                               "WHERE id=? AND call_id=? AND state='RESERVED'", (now, call["agent_id"], call["id"]))
            if cur.rowcount == 1:
                store.log_tr(conn, now, "agent", call["agent_id"], "RESERVED", "DIALING", "provider_accepted", self.worker)

    # ------------------------------------------------------------------ placement results
    def mark_initiated(self, call_id: str, provider: str) -> bool:
        now = self.clock.now()
        with self.db.tx() as conn:
            call = store.get_call(conn, call_id)
            if call is None:
                return False
            if call["provider"] is None:
                conn.execute("UPDATE calls SET provider=? WHERE id=? AND provider IS NULL", (provider, call_id))
            ok = store.set_call(conn, call_id, (C.RESERVED.value,), C.INITIATED.value, now, "provider_accepted",
                                self.worker, initiated_at=now)
            if ok:
                self._agent_dialing(conn, store.get_call(conn, call_id), now)
                bump(conn, call["campaign_id"], now, initiated=1)
            return ok

    def fail_placement(self, call_id: str, reason: str, provider: Optional[str], ambiguous: bool) -> bool:
        """place_call failed for good. If the outcome was ambiguous (timeout) the provider may still
        have the call, so a cancel is queued; if a ghost ANSWERED shows up later we hang it up too."""
        now = self.clock.now()
        with self.db.tx() as conn:
            call = store.get_call(conn, call_id)
            if call is None:
                return False
            ok = self._end(conn, call_id, C.FAILED, now, outcome="PLACEMENT_FAILED", reason=reason,
                           borrower_outcome="SYSTEM", cause="place_failed",
                           cmd="CANCEL" if ambiguous else None, provider=provider)
            if ok:
                bump(conn, call["campaign_id"], now, sys_fail=1)
            return ok

    def cancel_call(self, call_id: str, cause: str) -> bool:
        """We decide to stop an unanswered call (shedding, agent loss). No penalty to the borrower."""
        now = self.clock.now()
        with self.db.tx() as conn:
            call = store.get_call(conn, call_id)
            if call is None or call["state"] not in UNANSWERED:
                return False
            ok = self._end(conn, call_id, C.CANCELLED, now, outcome="CANCELLED", reason=cause,
                           borrower_outcome="CANCELLED", cause=cause, cmd="CANCEL")
            if ok:
                bump(conn, call["campaign_id"], now, cancelled=1)
            return ok

    # ------------------------------------------------------------------ provider events
    def handle_event(self, ev: ProviderEvent) -> str:
        now = self.clock.now()
        with self.db.tx() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO provider_events(provider,event_id,call_id,type,provider_ts,received_at,disposition) "
                               "VALUES(?,?,?,?,?,?,'PENDING')", (ev.provider, ev.event_id, ev.call_id, ev.type, ev.provider_ts, now))
            if cur.rowcount == 0:
                return "DUPLICATE"
            call = store.get_call(conn, ev.call_id)
            disp = "UNKNOWN_CALL" if call is None else self._apply(conn, call, ev, now)
            conn.execute("UPDATE provider_events SET disposition=? WHERE provider=? AND event_id=?",
                         (disp, ev.provider, ev.event_id))
            return disp

    def _apply(self, conn, call, ev: ProviderEvent, now) -> str:
        s, t, cid = C(call["state"]), ev.type, call["campaign_id"]
        if s in CALL_TERMINAL:
            if t == "ANSWERED" and s in (C.FAILED, C.CANCELLED):
                self._enqueue(conn, call, "HANGUP", now, ev.provider)
                bump(conn, cid, now, ghost_answers=1)
                return "GHOST_HANGUP"
            return "IGNORED_TERMINAL"
        rank = CALL_RANK[s]

        if t == "RINGING":
            if rank >= CALL_RANK[C.RINGING]:
                return "STALE"
            store.set_call(conn, call["id"], NON_TERMINAL, C.RINGING.value, now, "evt_ringing", self.worker)
            self._agent_dialing(conn, call, now)
            return "APPLIED"

        if t == "ANSWERED":
            if rank >= CALL_RANK[C.ANSWERED]:
                return "STALE"
            store.set_call(conn, call["id"], NON_TERMINAL, C.ANSWERED.value, now, "evt_answered", self.worker, answered_at=now)
            self._agent_dialing(conn, call, now)
            bump(conn, cid, now, resolved=1, answered=1, setup_sum=now - call["created_at"],
                 setup_sq=(now - call["created_at"]) ** 2, setup_n=1)
            self._bridge(conn, store.get_call(conn, call["id"]), now)
            return "APPLIED"

        if t in ("NO_ANSWER", "BUSY"):
            if rank >= CALL_RANK[C.ANSWERED]:
                return "STALE"
            self._end(conn, call["id"], C.FAILED, now, outcome=t, reason=t, borrower_outcome=t, cause="evt_" + t.lower())
            bump(conn, cid, now, resolved=1)
            return "APPLIED"

        if t == "FAILED":
            if rank >= CALL_RANK[C.ANSWERED]:        # line dropped after pickup
                return self._complete(conn, call, ev, now, outcome="DROPPED")
            self._end(conn, call["id"], C.FAILED, now, outcome="PROVIDER_FAILED", reason="provider_failed",
                      borrower_outcome="SYSTEM", cause="evt_failed")
            bump(conn, cid, now, sys_fail=1)
            return "APPLIED"

        if t == "COMPLETED":
            return self._complete(conn, call, ev, now, outcome="COMPLETED")
        return "IGNORED_UNKNOWN_TYPE"

    def _complete(self, conn, call, ev, now, outcome) -> str:
        s, cid = C(call["state"]), call["campaign_id"]
        if s == C.CONNECTED:
            talk = ev.talk_s if ev.talk_s > 0 else max(0.0, now - (call["connected_at"] or now))
            self._end(conn, call["id"], C.COMPLETED, now, outcome=outcome, reason=None, borrower_outcome="DONE",
                      cause="evt_completed", wrap=True)
            bump(conn, cid, now, talk_sum=talk, talk_n=1)
            return "APPLIED"
        if s == C.ANSWERED:
            # Borrower hung up while still waiting for an agent: that is an abandoned call.
            self._end(conn, call["id"], C.ABANDONED, now, outcome="ABANDONED", reason="borrower_hung_up_while_held",
                      borrower_outcome="ABANDONED", cause="evt_completed_while_held")
            bump(conn, cid, now, abandoned=1)
            return "APPLIED_ABANDONED"
        # COMPLETED arrived before we saw ANSWERED (out of order). The call is over; honour the
        # terminal fact, free the agent, and count the unbridged answer against the abandon budget.
        answered = ev.talk_s > 0
        self._end(conn, call["id"], C.COMPLETED, now, outcome="ANSWER_NOT_OBSERVED" if answered else outcome,
                  reason="completed_before_answered_seen", borrower_outcome="ABANDONED" if answered else "SYSTEM",
                  cause="evt_completed_early")
        if answered:
            bump(conn, cid, now, resolved=1, answered=1, abandoned=1)
        return "APPLIED_EARLY"

    # ------------------------------------------------------------------ bridging to an agent
    def _bridge(self, conn, call, now):
        """Borrower answered. Give them an agent now, or hold them for at most hold_grace_s."""
        cid = call["campaign_id"]
        if call["agent_id"]:
            cur = conn.execute("UPDATE agents SET state='CONNECTED', version=version+1, state_since=?, reserved_until=NULL "
                               "WHERE id=? AND call_id=? AND state IN ('RESERVED','DIALING')",
                               (now, call["agent_id"], call["id"]))
            if cur.rowcount == 1:
                store.log_tr(conn, now, "agent", call["agent_id"], "DIALING", "CONNECTED", "borrower_answered", self.worker)
                self._connect(conn, call, now, call["agent_id"])
                return
            conn.execute("UPDATE calls SET agent_id=NULL WHERE id=?", (call["id"],))   # agent vanished or was reassigned
        aid = store.claim_one_agent(conn, cid, call["id"], now, A.CONNECTED, self.worker, "late_bind")
        if aid:
            self._connect(conn, call, now, aid)
        else:
            conn.execute("UPDATE calls SET hold_deadline=? WHERE id=?", (now + self.hold_grace_s, call["id"]))
            bump(conn, cid, now, held=1)

    def _connect(self, conn, call, now, agent_id):
        ok = store.set_call(conn, call["id"], (C.ANSWERED.value,), C.CONNECTED.value, now, "bridged", self.worker,
                            agent_id=agent_id, connected_at=now, hold_deadline=None)
        if not ok:
            raise RuntimeError("call changed under the allocation lock")   # rolls the transaction back
        bump(conn, call["campaign_id"], now, connected=1)

    def serve_holds(self, conn, campaign_id, now) -> int:
        n = 0
        for r in conn.execute("SELECT id FROM calls WHERE campaign_id=? AND state='ANSWERED' AND agent_id IS NULL "
                              "ORDER BY answered_at LIMIT 50", (campaign_id,)).fetchall():
            aid = store.claim_one_agent(conn, campaign_id, r["id"], now, A.CONNECTED, self.worker, "serve_hold")
            if not aid:
                break
            self._connect(conn, store.get_call(conn, r["id"]), now, aid)
            n += 1
        return n

    def expire_holds(self, campaign_id) -> int:
        now = self.clock.now()
        n = 0
        with self.db.tx() as conn:
            self.serve_holds(conn, campaign_id, now)
            for r in conn.execute("SELECT id FROM calls WHERE campaign_id=? AND state='ANSWERED' AND agent_id IS NULL "
                                  "AND hold_deadline<=?", (campaign_id, now)).fetchall():
                if self._end(conn, r["id"], C.ABANDONED, now, outcome="ABANDONED", reason="no_agent_within_grace",
                             borrower_outcome="ABANDONED", cause="hold_expired", cmd="HANGUP"):
                    bump(conn, campaign_id, now, abandoned=1)
                    n += 1
        return n

    # ------------------------------------------------------------------ agents disappearing
    def orphan_of_agent(self, conn, agent_id, now) -> Optional[str]:
        """An agent vanished. Decide what happens to the call that was bound to it."""
        call = conn.execute("SELECT * FROM calls WHERE agent_id=? AND state IN ('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED')",
                            (agent_id,)).fetchone()
        if call is None:
            return None
        cid, s = call["campaign_id"], C(call["state"])
        to = A.RESERVED if s == C.RESERVED else (A.CONNECTED if s == C.CONNECTED else A.DIALING)
        new = store.claim_one_agent(conn, cid, call["id"], now, to, self.worker, "rebind")
        if new:
            conn.execute("UPDATE calls SET agent_id=? WHERE id=?", (new, call["id"]))
            return "REBOUND"
        if s == C.CONNECTED:       # borrower was mid-conversation and nobody can take over
            self._end(conn, call["id"], C.COMPLETED, now, outcome="AGENT_DROPPED", reason="agent_lost_mid_call",
                      borrower_outcome="ABANDONED", cause="agent_lost", cmd="HANGUP")
            bump(conn, cid, now, dropped=1)      # agent-side loss: reported separately, not a pacing abandon
            return "ENDED"
        # Unanswered: keep dialing but unbound. The Safety Controller's shed pass decides whether
        # the campaign can afford it; in progressive mode it will cancel it.
        conn.execute("UPDATE calls SET agent_id=NULL WHERE id=?", (call["id"],))
        return "UNBOUND"
