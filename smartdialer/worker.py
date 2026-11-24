"""A dialer worker. Run as many as you like against the same database.

Pipeline per campaign, per tick:

    Campaign -> Pacing Engine -> Safety Controller -> Call Allocator -> Telecom Provider
                (read-only,       (re-reads truth,     (writes, in the   (outside any
                 may be stale)     can cut or refuse)   same lock)        database lock)

Workers coordinate through the database only. A short dispatch lease keeps N workers from all doing
the same pacing work, but correctness never depends on it: the review and the reservation happen in
one write transaction, so two workers that both dispatch simply serialise and the second one sees
the first one's calls.
"""
import json
from typing import Dict, List, Optional

from . import store
from .agents import AgentService
from .allocator import CallAllocator, WorkerCrash
from .calls import CallService
from .clock import Clock
from .config import CampaignConfig, SafetyConfig
from .db import Database
from .housekeeping import Reconciler
from .models import ProviderEvent
from .pacing.predictive import PredictivePacing
from .pacing.progressive import ProgressivePacing
from .pacing.stats import read_stats
from .providers.router import ProviderRouter
from .safety import SafetyController


def bootstrap_campaign(db: Database, cfg: CampaignConfig, now: float, status: str = "RUNNING"):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO campaigns(id,name,mode,status) VALUES(?,?,?,?)", (cfg.id, cfg.name, cfg.mode, status))


class DialerWorker:
    def __init__(self, worker_id: str, db: Database, clock: Clock, router: ProviderRouter,
                 campaigns: Dict[str, CampaignConfig], safety_cfg: SafetyConfig, use_lease: bool = True,
                 recon_interval_s: float = 1.0):
        self.id, self.db, self.clock, self.router, self.campaigns = worker_id, db, clock, router, campaigns
        self.safety = SafetyController(safety_cfg)
        self.calls = CallService(db, clock, worker_id, campaigns, safety_cfg.hold_grace_s)
        self.agents = AgentService(db, clock, self.calls, worker_id)
        self.allocator = CallAllocator(self.calls, router, clock, campaigns, worker_id)
        self.recon = Reconciler(db, clock, self.calls, router, campaigns, worker_id)
        self.progressive = ProgressivePacing()
        self.predictive = {cid: PredictivePacing(c.pacing) for cid, c in campaigns.items()}
        self.use_lease = use_lease
        self.recon_interval_s = recon_interval_s
        self._last_recon = -1e9
        self.alive = True
        self.last_decision: Optional[dict] = None

    # ------------------------------------------------------------------ events
    def ingest(self, events: List[ProviderEvent]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for ev in events:
            d = self.calls.handle_event(ev)
            out[d] = out.get(d, 0) + 1
        return out

    # ------------------------------------------------------------------ one tick
    def tick(self, campaign_id: str):
        now = self.clock.now()
        self.agents.release_wrapups(campaign_id)
        self.calls.expire_holds(campaign_id)
        self.recon.send_commands()
        if now - self._last_recon >= self.recon_interval_s:
            self._last_recon = now
            self.recon.run(campaign_id)
        self._note_availability(campaign_id)
        self.shed(campaign_id)
        self.dispatch(campaign_id)

    def _campaign(self, conn, cid):
        return conn.execute("SELECT mode, status FROM campaigns WHERE id=?", (cid,)).fetchone()

    def _note_availability(self, cid):
        now = self.clock.now()
        with self.db.tx() as conn:
            snap = store.take_snapshot(conn, cid, now, 10.0)
            self.safety.note_availability(conn, cid, snap, now)

    # ------------------------------------------------------------------ shedding
    def shed(self, cid) -> int:
        """If the Safety Controller says fewer unbound calls may be in flight than there are
        (agents vanished, answer rate jumped, fallback tripped), cancel the newest ones."""
        now = self.clock.now()
        conn = self.db.conn()
        camp = self._campaign(conn, cid)
        snap = store.take_snapshot(conn, cid, now, 10.0)
        if snap.unanswered_overflow == 0:
            return 0
        with self.db.tx() as c2:
            allowance = self.safety.overflow_allowance(c2, cid, camp["mode"], snap, now)
        excess = snap.unanswered_overflow - allowance
        if excess <= 0 or (allowance > 0 and excess <= 0.5 * allowance + 5):    # hysteresis: do not churn on noise
            return 0
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM calls WHERE campaign_id=? AND agent_id IS NULL AND state IN ('INITIATED','RINGING') "
            "ORDER BY created_at DESC LIMIT ?", (cid, excess))]
        n = sum(1 for i in ids if self.calls.cancel_call(i, "shed"))
        return n

    # ------------------------------------------------------------------ dispatch
    def _lease(self, cid, now, ttl=0.4) -> bool:
        if not self.use_lease:
            return True
        with self.db.tx() as c:
            cur = c.execute("UPDATE campaigns SET dispatch_owner=?, dispatch_until=? WHERE id=? AND (dispatch_until<=? OR dispatch_owner=?)",
                            (self.id, now + ttl, cid, now, self.id))
            return cur.rowcount == 1

    def dispatch(self, cid) -> Optional[dict]:
        now = self.clock.now()
        cfg = self.campaigns[cid]
        conn = self.db.conn()
        camp = self._campaign(conn, cid)
        if camp is None or camp["status"] != "RUNNING":
            return None
        if not self._lease(cid, now):
            return None
        mode = camp["mode"]
        if mode == "PREDICTIVE":      # the controller says which engine is allowed to speak right now
            with self.db.tx() as cm:
                if self.safety.effective_mode(cm, cid, mode, now) == "PROGRESSIVE":
                    mode = "PROGRESSIVE_FALLBACK"
        # --- 1. pacing: read-only inputs, no lock, may be stale by the time we get the lock
        if mode == "PREDICTIVE":
            eng = self.predictive[cid]
            stats = read_stats(conn, cid, now, cfg.pacing)
            snap0 = store.take_snapshot(conn, cid, now, eng.horizon(stats))
            proposal = eng.propose(snap0, stats, now)
            horizon = eng.horizon(stats)
        else:
            snap0 = store.take_snapshot(conn, cid, now, 10.0)
            proposal = self.progressive.propose(snap0, now)
            if mode == "PROGRESSIVE_FALLBACK":
                proposal.mode = "PREDICTIVE"       # still a predictive campaign; the controller decides it behaves progressively
            horizon = 10.0
        if proposal.want <= 0:
            return None
        # --- 2. safety review + reservation, atomically
        with self.db.tx() as c2:
            now2 = self.clock.now()
            snap = store.take_snapshot(c2, cid, now2, self.safety.horizon_s)   # controller's own horizon, not pacing's
            decision = self.safety.review(c2, cid, proposal, snap, self.router.health(), now2)
            created = self.allocator.allocate(c2, decision, now2)
            rec = dict(ts=now2, worker=self.id, mode=proposal.mode, requested=decision.requested, approved=len(created),
                       bound=sum(1 for x in created if x.bound), overflow=sum(1 for x in created if not x.bound),
                       verdict=decision.verdict, effective_mode=decision.effective_mode,
                       reasons=[r.__dict__ for r in decision.rules], explain=proposal.explain)
            c2.execute("INSERT INTO decisions(ts,campaign_id,worker,mode,requested,approved,bound,overflow,verdict,effective_mode,reasons,explain) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (rec["ts"], cid, self.id, rec["mode"], rec["requested"], rec["approved"], rec["bound"], rec["overflow"],
                        rec["verdict"], rec["effective_mode"], json.dumps(rec["reasons"]), json.dumps(rec["explain"])))
        self.last_decision = rec
        # --- 3. provider, outside the lock
        self.allocator.place(created)
        return rec
