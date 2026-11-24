"""Small deterministic environment for unit tests: one campaign, a provider we control by hand."""
from typing import List, Optional

from smartdialer import store
from smartdialer.clock import SimClock
from smartdialer.config import CampaignConfig, SafetyConfig
from smartdialer.db import Database
from smartdialer.models import DialRequest, ProviderCallStatus, ProviderError, ProviderEvent, ProviderTimeout
from smartdialer.providers.base import TelecomProvider
from smartdialer.providers.router import ProviderRouter
from smartdialer.worker import DialerWorker, bootstrap_campaign


class ScriptedProvider(TelecomProvider):
    """Accepts calls and says nothing until the test injects events."""

    def __init__(self, name="A"):
        self.name = name
        self.placed: List[str] = []
        self.cancelled: List[str] = []
        self.statuses = {}
        self.queue: List[ProviderEvent] = []
        self.fail: Optional[Exception] = None          # raised by place_call while set
        self.unreachable = False

    def place_call(self, req: DialRequest):
        if self.fail:
            raise self.fail
        if req.call_id not in self.placed:
            self.placed.append(req.call_id)

    def cancel_call(self, call_id):
        if self.unreachable:
            raise ProviderTimeout("down")
        self.cancelled.append(call_id)

    def get_call_status(self, call_id):
        if self.unreachable:
            raise ProviderTimeout("down")
        if call_id in self.statuses:
            return self.statuses[call_id]
        return ProviderCallStatus(call_id in self.placed, "RINGING" if call_id in self.placed else "")

    def poll_events(self):
        q, self.queue = self.queue, []
        return q


class Env:
    def __init__(self, tmp_path, agents=2, borrowers=20, mode="PROGRESSIVE", providers=None, safety=None):
        self.clock = SimClock(1000.0)
        self.db = Database(str(tmp_path / "t.db"))
        self.safety_cfg = safety or SafetyConfig()
        self.cfg = CampaignConfig(mode=mode)
        self.cid = self.cfg.id
        self.providers = providers or [ScriptedProvider("A")]
        self.router = ProviderRouter(self.providers, self.clock, self.safety_cfg)
        self.worker = DialerWorker("w1", self.db, self.clock, self.router, {self.cid: self.cfg}, self.safety_cfg, use_lease=False)
        bootstrap_campaign(self.db, self.cfg, 0)
        with self.db.tx() as c:
            store.add_borrowers(c, self.cid, [(f"b{i}", f"+9100{i}", 0) for i in range(borrowers)])
        self.agent_ids = [f"ag{i}" for i in range(agents)]
        for a in self.agent_ids:
            self.worker.agents.login(self.cid, a)

    @property
    def p(self):
        return self.providers[0]

    def dispatch(self):
        return self.worker.dispatch(self.cid)

    def event(self, call_id, typ, talk=0.0, eid=None, provider=None):
        eid = eid or f"e{len(self.db.conn().execute('SELECT 1 FROM provider_events').fetchall()) + 1}"
        return self.worker.calls.handle_event(ProviderEvent(provider or self.p.name, eid, call_id, typ, self.clock.now(), talk))

    def call(self, call_id):
        return self.db.conn().execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()

    def agent(self, agent_id):
        return self.db.conn().execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()

    def calls_in(self, *states):
        q = ",".join("?" * len(states))
        return [r["id"] for r in self.db.conn().execute(f"SELECT id FROM calls WHERE state IN ({q}) ORDER BY created_at, id", states)]

    def transitions(self, entity, eid):
        return [(r["from_state"], r["to_state"]) for r in
                self.db.conn().execute("SELECT from_state,to_state FROM transitions WHERE entity=? AND entity_id=? ORDER BY id", (entity, eid))]

    def states_of_agents(self):
        return {r["id"]: r["state"] for r in self.db.conn().execute("SELECT id,state FROM agents")}

    def tick(self, dt=0.5):
        self.clock.advance(dt)
        self.worker.tick(self.cid)

    def snapshot(self):
        return store.take_snapshot(self.db.conn(), self.cid, self.clock.now(), 10.0)
