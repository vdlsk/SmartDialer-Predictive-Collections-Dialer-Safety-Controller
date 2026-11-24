"""Deterministic simulation harness. One object wires a database, a virtual clock, two mock
providers and N dialer workers, and steps them in lock-step. The dashboard, the CLI scripts and the
tests all drive this same class."""
import json
import os
import random
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from .. import store
from ..allocator import WorkerCrash
from ..clock import SimClock
from ..config import CampaignConfig, SafetyConfig
from ..db import Database
from ..pacing.stats import read_stats
from ..providers.mock import BorrowerModel, provider_a, provider_b
from ..providers.router import ProviderRouter
from ..worker import DialerWorker, bootstrap_campaign
from .world import SCENARIOS, Scenario, stress

T0 = 1_000_000.0


class Simulation:
    def __init__(self, *, agents: int = 100, mode: str = "PREDICTIVE", scenario: str = "B", workers: int = 1,
                 borrowers: int = 20000, seed: int = 1, tick_s: float = 0.5, stressed: bool = False,
                 weights: Optional[Dict[str, float]] = None, safety: Optional[SafetyConfig] = None,
                 db_path: Optional[str] = None, use_lease: bool = True, sample_s: float = 1.0,
                 pacing=None):
        self.tick_s, self.sample_s = tick_s, sample_s
        self.clock = SimClock(T0)
        self.t0 = T0
        self.scn = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
        ar, tm = self.scn.build(T0)
        self.model = BorrowerModel(ar, tm, seed=seed)
        if db_path is None:
            self._tmp = tempfile.mkdtemp(prefix="smartdialer_")
            db_path = os.path.join(self._tmp, "sim.db")
        self.db = Database(db_path)
        self.safety_cfg = safety or SafetyConfig()
        self.cfg = CampaignConfig(mode=mode)
        if pacing is not None:
            self.cfg.pacing = pacing
        self.campaigns = {self.cfg.id: self.cfg}
        self.cid = self.cfg.id
        self.A, self.B = provider_a(self.clock, self.model, seed + 10), provider_b(self.clock, self.model, seed + 20)
        if stressed:
            stress(self.A.p)
            stress(self.B.p)
        self.providers = {"A": self.A, "B": self.B}
        self.weights = weights or {"A": 0.65, "B": 0.35}
        self.use_lease = use_lease
        self.workers: List[DialerWorker] = []
        self._wseq = 0
        self.rng = random.Random(seed + 99)
        bootstrap_campaign(self.db, self.cfg, T0)
        with self.db.tx() as c:
            rr = random.Random(seed)
            store.add_borrowers(c, self.cid, [(f"b{i}", f"+91{9000000000 + i}", rr.randint(0, 100)) for i in range(borrowers)])
        for _ in range(workers):
            self.add_worker()
        self.agent_ids = [f"ag{i:05d}" for i in range(agents)]
        self.dropped: List[str] = []
        for a in self.agent_ids:
            self.workers[0].agents.login(self.cid, a)
        self.timeline: List[Tuple[float, str, Callable]] = []
        self.events_log: List[dict] = []
        self.event_counts: Dict[str, int] = {}
        self.crashes = 0
        self.metrics: List[dict] = []
        self._next_sample = 0.0
        self._rot = 0
        self._last_dec_id = 0

    # ------------------------------------------------------------------ wiring
    def add_worker(self) -> DialerWorker:
        self._wseq += 1
        router = ProviderRouter(list(self.providers.values()), self.clock, self.safety_cfg, self.weights, seed=self._wseq)
        w = DialerWorker(f"w{self._wseq}", self.db, self.clock, router, self.campaigns, self.safety_cfg, self.use_lease)
        self.workers.append(w)
        return w

    @property
    def t(self) -> float:
        return self.clock.now() - self.t0

    def live(self) -> List[DialerWorker]:
        return [w for w in self.workers if w.alive]

    def log(self, kind: str, text: str):
        self.events_log.append({"t": round(self.t, 1), "kind": kind, "text": text})
        del self.events_log[:-200]

    # ------------------------------------------------------------------ scripted actions
    def at(self, t: float, label: str, fn: Callable):
        self.timeline.append((t, label, fn))
        self.timeline.sort(key=lambda x: x[0])

    def set_mode(self, mode: str):
        with self.db.tx() as c:
            c.execute("UPDATE campaigns SET mode=? WHERE id=?", (mode, self.cid))
        self.log("control", f"campaign mode -> {mode}")

    def set_status(self, status: str):
        with self.db.tx() as c:
            c.execute("UPDATE campaigns SET status=? WHERE id=?", (status, self.cid))

    def set_scenario(self, key: str):
        self.scn = SCENARIOS[key]
        ar, tm = self.scn.build(self.clock.now())
        self.model.answer_rate, self.model.talk_mean_s = ar, tm
        self.log("control", f"scenario {key}: {self.scn.note}")

    def set_rates(self, answer_rate: Optional[float] = None, talk_mean_s: Optional[float] = None):
        if answer_rate is not None:
            self.model.answer_rate = float(answer_rate)
        if talk_mean_s is not None:
            self.model.talk_mean_s = float(talk_mean_s)
        self.log("control", f"answer rate {self.model.answer_rate if not callable(self.model.answer_rate) else 'scripted'}, talk {self.model.talk_mean_s if not callable(self.model.talk_mean_s) else 'scripted'}")

    def drop_agents(self, k: int) -> int:
        conn = self.db.conn()
        online = [r["id"] for r in conn.execute("SELECT id FROM agents WHERE state NOT IN ('OFFLINE','PAUSED')")]
        self.rng.shuffle(online)
        w = self.live()[0]
        n = 0
        for a in online[:k]:
            w.agents.disconnect(a)
            self.dropped.append(a)
            n += 1
        self.log("fault", f"{n} agents disconnected at once")
        return n

    def restore_agents(self) -> int:
        w = self.live()[0]
        n = len(self.dropped)
        for a in self.dropped:
            w.agents.login(self.cid, a)
        self.dropped = []
        self.log("control", f"{n} agents logged back in")
        return n

    def add_agents(self, k: int):
        base = len(self.agent_ids)
        for i in range(k):
            a = f"ag{base + i:05d}"
            self.agent_ids.append(a)
            self.live()[0].agents.login(self.cid, a)
        self.log("control", f"{k} agents joined")

    def provider_outage(self, name: str, on: bool):
        self.providers[name].set_outage(on)
        self.log("fault", f"provider {name} {'OUTAGE started' if on else 'recovered'}")

    def crash_worker(self, point: str = "after_place"):
        w = self.live()[0]
        w.allocator.crash_at = point
        self.log("fault", f"worker {w.id} will crash {point.replace('_', ' ')}")

    def restart_worker(self):
        w = self.add_worker()
        self.log("control", f"worker {w.id} started")
        return w

    # ------------------------------------------------------------------ stepping
    def step(self):
        self.clock.advance(self.tick_s)
        while self.timeline and self.timeline[0][0] <= self.t:
            _, label, fn = self.timeline.pop(0)
            self.log("script", label)
            fn(self)
        live = self.live()
        if not live:
            self.restart_worker()
            live = self.live()
        # provider -> webhook deliveries, spread over live workers
        for p in self.providers.values():
            for ev in p.poll_events():
                w = live[self.rng.randrange(len(live))]
                d = w.calls.handle_event(ev)
                self.event_counts[d] = self.event_counts.get(d, 0) + 1
        # ticks, rotating which worker goes first so the dispatch lease moves around
        self._rot += 1
        for i in range(len(live)):
            w = live[(i + self._rot) % len(live)]
            try:
                w.tick(self.cid)
            except WorkerCrash as e:
                w.alive = False
                self.crashes += 1
                self.log("fault", f"worker {w.id} CRASHED: {e}")
        if self.t >= self._next_sample:
            self._next_sample = self.t + self.sample_s
            self.metrics.append(self.sample())

    def run(self, seconds: float):
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.step()

    # ------------------------------------------------------------------ measurements
    def totals(self) -> Dict[str, float]:
        return {r["key"]: r["val"] for r in self.db.conn().execute(
            "SELECT key, SUM(val) val FROM buckets WHERE campaign_id=? GROUP BY key", (self.cid,))}

    def sample(self) -> dict:
        conn = self.db.conn()
        now = self.clock.now()
        snap = store.take_snapshot(conn, self.cid, now, 10.0)
        tot = self.totals()
        decs = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(requested),0) req, COALESCE(SUM(approved),0) app, MAX(id) mx "
                            "FROM decisions WHERE id>?", (self._last_dec_id,)).fetchone()
        if decs["mx"]:
            self._last_dec_id = decs["mx"]
        pt = self.model.answer_rate(now) if callable(self.model.answer_rate) else self.model.answer_rate
        st = read_stats(conn, self.cid, now, self.cfg.pacing)
        health = self.workers[-1].router.health()
        return dict(
            t=round(self.t, 1), online=snap.online, available=snap.available, reserved=snap.reserved, dialing=snap.dialing,
            connected=snap.connected, wrap_up=snap.wrap_up, paused=snap.paused, offline=snap.offline,
            unanswered_bound=snap.unanswered_bound, unanswered_overflow=snap.unanswered_overflow, held=snap.held,
            util=snap.connected / snap.online if snap.online else 0.0,
            occ=(snap.connected + snap.wrap_up) / snap.online if snap.online else 0.0,
            initiated=tot.get("initiated", 0), answered=tot.get("answered", 0), connected_total=tot.get("connected", 0),
            abandoned=tot.get("abandoned", 0), sys_fail=tot.get("sys_fail", 0), cancelled=tot.get("cancelled", 0),
            p_true=round(pt, 3), p_est=round(st.p_fast, 3), p_plan=round(st.p_upper, 3),
            requested=decs["req"], approved=decs["app"], decisions=decs["n"],
            provider_health={k: v["state"] for k, v in health.items()},
        )

    def summary(self, warmup_s: float = 120.0) -> dict:
        ms = [m for m in self.metrics if m["t"] >= warmup_s]
        if not ms:
            ms = self.metrics
        tot = self.totals()
        first, last = ms[0], ms[-1]
        d = lambda k: last[k] - first[k]
        conn = self.db.conn()
        verdicts = {r["verdict"]: r["n"] for r in conn.execute("SELECT verdict, COUNT(*) n FROM decisions GROUP BY verdict")}
        rules = {}
        for r in conn.execute("SELECT json_extract(j.value,'$.rule') rule, json_extract(j.value,'$.effect') eff, COUNT(*) n "
                              "FROM decisions, json_each(decisions.reasons) j WHERE json_extract(j.value,'$.effect') != 'PASS' "
                              "GROUP BY 1,2 ORDER BY n DESC"):
            rules[f"{r['rule']}:{r['eff']}"] = r["n"]
        conn_calls = d("connected_total")
        ab = d("abandoned")
        answered_calls = conn_calls + ab
        return dict(
            window_s=round(last["t"] - first["t"]),
            talk_utilization=round(sum(m["util"] for m in ms) / len(ms), 3),
            occupancy=round(sum(m["occ"] for m in ms) / len(ms), 3),
            calls_initiated=int(d("initiated")), calls_answered=int(d("answered")), calls_connected=int(conn_calls),
            abandoned=int(ab), abandon_rate=round(ab / answered_calls, 4) if answered_calls else 0.0,
            dials_per_connect=round(d("initiated") / conn_calls, 2) if conn_calls else None,
            peak_in_flight=max(m["unanswered_bound"] + m["unanswered_overflow"] for m in ms),
            sys_failures=int(d("sys_fail")), cancelled=int(d("cancelled")),
            verdicts=verdicts, rule_actions=rules, event_dispositions=dict(self.event_counts),
            recon={k: sum(w.recon.counters[k] for w in self.workers) for k in self.workers[0].recon.counters},
            totals_all_time={k: int(v) for k, v in tot.items()},
        )

    def invariants(self) -> List[str]:
        """Things that must be true at every instant. Returns a list of violations (empty = healthy)."""
        c = self.db.conn()
        bad = []
        for r in c.execute("SELECT agent_id, COUNT(*) n FROM calls WHERE agent_id IS NOT NULL AND state IN "
                           "('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED') GROUP BY agent_id HAVING n>1"):
            bad.append(f"agent {r['agent_id']} bound to {r['n']} active calls")
        for r in c.execute("SELECT call_id, COUNT(*) n FROM agents WHERE call_id IS NOT NULL GROUP BY call_id HAVING n>1"):
            bad.append(f"call {r['call_id']} owns {r['n']} agents")
        for r in c.execute("SELECT borrower_id, COUNT(*) n FROM calls WHERE state IN ('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED') "
                           "GROUP BY borrower_id HAVING n>1"):
            bad.append(f"borrower {r['borrower_id']} has {r['n']} active calls")
        for r in c.execute("SELECT a.id, a.state, a.call_id, c.state cs, c.agent_id FROM agents a JOIN calls c ON c.id=a.call_id "
                           "WHERE a.call_id IS NOT NULL AND (c.agent_id IS NOT a.id OR c.state IN ('COMPLETED','FAILED','CANCELLED','ABANDONED'))"):
            bad.append(f"agent {r['id']} ({r['state']}) points at call {r['call_id']} ({r['cs']}, agent={r['agent_id']})")
        for r in c.execute("SELECT id FROM agents WHERE state IN ('RESERVED','DIALING','CONNECTED') AND call_id IS NULL"):
            bad.append(f"agent {r['id']} busy without a call")
        for r in c.execute("SELECT id FROM agents WHERE state IN ('AVAILABLE','WRAP_UP','PAUSED','OFFLINE') AND call_id IS NOT NULL"):
            bad.append(f"agent {r['id']} idle but holds a call")
        # no illegal transitions in the audit log
        from ..states import AGENT_TRANSITIONS, CALL_TRANSITIONS, AgentState, CallState
        for r in c.execute("SELECT entity, entity_id, from_state, to_state FROM transitions"):
            if r["from_state"] in (None, "QUEUED"):
                continue
            tbl = AGENT_TRANSITIONS if r["entity"] == "agent" else CALL_TRANSITIONS
            enum = AgentState if r["entity"] == "agent" else CallState
            try:
                if enum(r["to_state"]) not in tbl[enum(r["from_state"])]:
                    bad.append(f"illegal {r['entity']} transition {r['from_state']}->{r['to_state']} ({r['entity_id']})")
            except ValueError:
                bad.append(f"unknown state in transition {r}")
        return bad[:20]
