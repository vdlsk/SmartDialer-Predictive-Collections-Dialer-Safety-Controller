"""The five failure cases from the brief, plus an answer-rate shock, each as a scripted run.
Every drill returns the facts it measured; tests/test_drills.py asserts on them and
scripts/failure_drills.py prints them. Nothing here reaches into the engine's internals
except to read state."""
from typing import Dict

from .harness import Simulation


def _count(sim, sql, *a):
    return sim.db.conn().execute(sql, a).fetchone()[0]


def _series(sim, key, t0, t1):
    return [(m["t"], m[key]) for m in sim.metrics if t0 <= m["t"] <= t1]


# --------------------------------------------------------------------------------------------- 1
def worker_crash(point: str = "after_place", seed: int = 4) -> Dict:
    """Agent reserved > borrower reserved > call initiated > worker crashes."""
    sim = Simulation(agents=60, mode="PROGRESSIVE", scenario="B", workers=2, seed=seed)
    sim.run(60)
    sim.crash_worker(point)
    crash_t = None
    for _ in range(400):
        sim.step()
        if sim.crashes:
            crash_t = sim.t
            break
    assert crash_t is not None, "worker never crashed"
    c = sim.db.conn()
    orphans = [r["id"] for r in c.execute("SELECT id FROM calls WHERE state='RESERVED'")]
    reserved_agents = _count(sim, "SELECT COUNT(*) FROM agents WHERE state='RESERVED'")
    # the crashed worker stays dead; worker 2 carries on alone
    healed_at, t_end = None, crash_t + 40
    while sim.t < t_end:
        sim.step()
        left = [i for i in orphans if sim.db.conn().execute("SELECT state FROM calls WHERE id=?", (i,)).fetchone()["state"] == "RESERVED"]
        if not left and healed_at is None:
            healed_at = sim.t - crash_t
    fates = {}
    for i in orphans:
        row = sim.db.conn().execute("SELECT state, outcome FROM calls WHERE id=?", (i,)).fetchone()
        key = f"{row['state']}" + (f"/{row['outcome']}" if row["outcome"] else "")
        fates[key] = fates.get(key, 0) + 1
    sim.run(60)
    return dict(
        point=point, crash_at_s=round(crash_t, 1), calls_left_reserved=len(orphans), agents_left_reserved=reserved_agents,
        healed_after_s=round(healed_at, 1) if healed_at is not None else None, fate_of_orphaned_calls=fates,
        reconciler_adopted=sum(w.recon.counters["adopted"] for w in sim.workers),
        reconciler_released=sum(w.recon.counters["released"] for w in sim.workers),
        calls_still_reserved_at_end=_count(sim, "SELECT COUNT(*) FROM calls WHERE state='RESERVED'"),
        borrowers_with_two_live_calls=_count(sim, "SELECT COUNT(*) FROM (SELECT borrower_id FROM calls WHERE state IN ('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED') GROUP BY borrower_id HAVING COUNT(*)>1)"),
        invariant_violations=sim.invariants(), live_workers=len(sim.live()),
        calls_started_after_crash=int(sim.metrics[-1]["initiated"] - [m for m in sim.metrics if m["t"] <= crash_t][-1]["initiated"]),
    )


# --------------------------------------------------------------------------------------------- 2
def provider_outage(both: bool = False, seed: int = 5, mode: str = "PREDICTIVE") -> Dict:
    """Provider starts timing out. What happens to existing calls, new calls, retries, pacing?"""
    sim = Simulation(agents=100, mode=mode, scenario="B", workers=2, seed=seed, weights={"A": 0.5, "B": 0.5})
    sim.run(150)
    pre = dict(A=sim.A.stats["placed"], B=sim.B.stats["placed"])
    in_flight_at_start = _count(sim, "SELECT COUNT(*) FROM calls WHERE state IN ('INITIATED','RINGING')")
    t_out = sim.t
    sim.provider_outage("A", True)
    if both:
        sim.provider_outage("B", True)
    dec0 = _count(sim, "SELECT MAX(id) FROM decisions") or 0
    placed0 = dict(A=sim.A.stats["placed"], B=sim.B.stats["placed"])
    sys0 = sim.totals().get("sys_fail", 0)
    breaker_open_at = None
    for _ in range(int(60 / sim.tick_s)):
        sim.step()
        if breaker_open_at is None and any(w.router.breakers["A"].state == "OPEN" for w in sim.live()):
            breaker_open_at = sim.t - t_out
    during = dict(A=sim.A.stats["placed"] - placed0["A"], B=sim.B.stats["placed"] - placed0["B"])
    verdicts = {r["verdict"]: r["n"] for r in sim.db.conn().execute(
        "SELECT verdict, COUNT(*) n FROM decisions WHERE id>? GROUP BY verdict", (dec0,))}
    stuck_ringing = _count(sim, "SELECT COUNT(*) FROM calls WHERE state IN ('INITIATED','RINGING') AND state_since < ?", sim.clock.now() - 40)
    sys_during = sim.totals().get("sys_fail", 0) - sys0
    cancel_backlog = _count(sim, "SELECT COUNT(*) FROM provider_commands WHERE status='PENDING'")
    sim.provider_outage("A", False)
    if both:
        sim.provider_outage("B", False)
    t_rec = sim.t
    placed1 = dict(A=sim.A.stats["placed"], B=sim.B.stats["placed"])
    resumed_at = None
    for _ in range(int(120 / sim.tick_s)):
        sim.step()
        if resumed_at is None and sim.A.stats["placed"] > placed1["A"]:
            resumed_at = sim.t - t_rec
    util_before = sum(m["util"] for m in _m(sim, 90, 150)) / max(1, len(_m(sim, 90, 150)))
    util_after = sum(m["util"] for m in _m(sim, t_rec + 60, t_rec + 120)) / max(1, len(_m(sim, t_rec + 60, t_rec + 120)))
    return dict(
        both_providers=both, in_flight_when_it_started=in_flight_at_start, breaker_opened_after_s=round(breaker_open_at, 1) if breaker_open_at else None,
        placed_during_outage=during, decisions_during_outage=verdicts, calls_failed_at_placement=int(sys_during),
        calls_stuck_ringing_gt_40s=stuck_ringing, cancel_commands_waiting=cancel_backlog,
        provider_A_used_again_after_s=round(resumed_at, 1) if resumed_at is not None else None,
        utilization_before=round(util_before, 3), utilization_recovered=round(util_after, 3),
        abandon_rate_overall=sim.summary(60)["abandon_rate"], invariant_violations=sim.invariants(),
        final_breakers=sim.workers[-1].router.health(),
    )


def _m(sim, a, b):
    return [m for m in sim.metrics if a <= m["t"] <= b]


# --------------------------------------------------------------------------------------------- 3
def agent_drop(drop: int = 40, seed: int = 6, mode: str = "PREDICTIVE") -> Dict:
    """100 agents available, 40 vanish within a moment."""
    sim = Simulation(agents=100, mode=mode, scenario="B", workers=2, seed=seed)
    sim.run(200)
    before = sim.metrics[-1]
    ab0 = sim.totals().get("abandoned", 0)
    can0 = sim.totals().get("cancelled", 0)
    t_drop = sim.t
    sim.drop_agents(drop)
    first_adjust = None
    series = []
    for _ in range(int(60 / sim.tick_s)):
        sim.step()
        s = sim.metrics[-1]
        series.append((round(sim.t - t_drop, 1), s["online"], s["unanswered_bound"] + s["unanswered_overflow"], s["held"]))
        online = s["online"]
        if first_adjust is None and s["unanswered_bound"] + s["unanswered_overflow"] <= SafetyBound.ratio(sim) * online and sim.t - t_drop > 0:
            first_adjust = sim.t - t_drop
    ab1 = sim.totals().get("abandoned", 0)
    after = _m(sim, t_drop + 30, t_drop + 60)
    snap_ds = [x for x in series if x[0] <= 20]
    return dict(
        online_before=before["online"], online_after=sim.metrics[-1]["online"], dropped=drop,
        in_flight_before=before["unanswered_bound"] + before["unanswered_overflow"],
        in_flight_peak_after_drop=max(x[2] for x in series[:8]), in_flight_after_10s=next((x[2] for x in series if x[0] >= 10), None),
        in_flight_after_30s=next((x[2] for x in series if x[0] >= 30), None),
        shed_calls=int(sim.totals().get("cancelled", 0) - can0), abandoned_during_drop=int(ab1 - ab0),
        abandon_rate_after=sim.summary(t_drop)["abandon_rate"],
        safety_rules_fired=[k for k in sim.summary(t_drop)["rule_actions"]],
        utilization_after=round(sum(m["util"] for m in after) / max(1, len(after)), 3),
        invariant_violations=sim.invariants(), series=series[::4],
    )


class SafetyBound:
    @staticmethod
    def ratio(sim):
        return sim.safety_cfg.hard_ceiling_ratio


# --------------------------------------------------------------------------------------------- 4, 5
def messy_provider(dup_rate=0.5, reorder_rate=0.4, seed: int = 7) -> Dict:
    """Same event many times, events in the wrong order. Everything routed through the sloppy provider."""
    sim = Simulation(agents=60, mode="PREDICTIVE", scenario="B", workers=3, seed=seed, weights={"A": 0.0001, "B": 1.0})
    p = sim.B.p
    p.dup_rate, p.reorder_rate, p.reorder_delay_s = dup_rate, reorder_rate, (1.0, 45.0)
    p.timeout_rate = 0.0
    sim.run(400)
    c = sim.db.conn()
    disp = dict(sim.event_counts)
    # transitions per call: each (from,to) pair at most once, and never more than the legal path length
    repeats = c.execute("SELECT COUNT(*) FROM (SELECT entity_id, from_state, to_state, COUNT(*) n FROM transitions "
                        "WHERE entity='call' GROUP BY 1,2,3 HAVING n>1)").fetchone()[0]
    max_per_call = c.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM transitions WHERE entity='call' GROUP BY entity_id)").fetchone()[0]
    calls_with_connect = c.execute("SELECT COUNT(*) FROM calls WHERE connected_at IS NOT NULL").fetchone()[0]
    connect_transitions = c.execute("SELECT COUNT(*) FROM transitions WHERE entity='call' AND to_state='CONNECTED'").fetchone()[0]
    completed_before_answer = c.execute("SELECT COUNT(*) FROM calls WHERE outcome='ANSWER_NOT_OBSERVED'").fetchone()[0]
    return dict(
        events_by_disposition=disp, repeated_transitions=repeats, max_transitions_on_one_call=max_per_call,
        calls_connected=calls_with_connect, connect_transitions=connect_transitions,
        completed_before_answered_seen=completed_before_answer, provider_B=dict(sim.B.stats),
        invariant_violations=sim.invariants(), summary={k: v for k, v in sim.summary(60).items() if k in ("talk_utilization", "abandon_rate", "calls_connected")},
        stuck_connected=c.execute("SELECT COUNT(*) FROM calls WHERE state='CONNECTED' AND connected_at < ?", (sim.clock.now() - 1200,)).fetchone()[0],
    )


# --------------------------------------------------------------------------------------------- 6
def answer_rate_shock(seed: int = 8) -> Dict:
    """The model believes 70% and reality becomes 10%; then the reverse, which is the dangerous one."""
    sim = Simulation(agents=100, mode="PREDICTIVE", scenario="C", workers=1, seed=seed)
    sim.run(180)
    base = sim.summary(60)
    # down: 70% -> 10%
    sim.set_rates(answer_rate=0.10, talk_mean_s=120)
    t_down = sim.t
    sim.run(150)
    down = _m(sim, t_down + 90, t_down + 150)
    est_follow = next((m["t"] - t_down for m in _m(sim, t_down, t_down + 150) if m["p_est"] <= 0.25), None)
    ab_before_up = sim.totals().get("abandoned", 0)
    conn_before_up = sim.totals().get("connected", 0)
    # up: 10% -> 70%
    sim.set_rates(answer_rate=0.70, talk_mean_s=120)
    t_up = sim.t
    peak_held = 0
    for _ in range(int(120 / sim.tick_s)):
        sim.step()
        peak_held = max(peak_held, sim.metrics[-1]["held"])
    ab_up = sim.totals().get("abandoned", 0) - ab_before_up
    conn_up = sim.totals().get("connected", 0) - conn_before_up
    up = _m(sim, t_up, t_up + 120)
    plan_follow = next((m["t"] - t_up for m in up if m["p_plan"] >= 0.5), None)
    return dict(
        utilization_steady_at_70pct=base["talk_utilization"],
        after_drop_to_10pct=dict(utilization=round(sum(m["util"] for m in down) / len(down), 3),
                                  estimate_followed_after_s=round(est_follow, 1) if est_follow is not None else None),
        after_jump_to_70pct=dict(abandoned=int(ab_up), connected=int(conn_up),
                                 abandon_rate=round(ab_up / max(1, ab_up + conn_up), 4), peak_answered_calls_waiting=peak_held,
                                 planning_rate_reached_50pct_after_s=round(plan_follow, 1) if plan_follow is not None else None,
                                 safety_rules_fired=[k for k in sim.summary(t_up)["rule_actions"]]),
        invariant_violations=sim.invariants(),
    )
