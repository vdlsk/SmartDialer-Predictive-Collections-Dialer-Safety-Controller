"""The failure cases from the brief, run end to end on the simulator. Slowest tests in the suite (~25 s)."""
import pytest

from smartdialer.sim import drills
from smartdialer.sim.harness import Simulation


def test_worker_crash_after_provider_accepted():
    r = drills.worker_crash("after_place")
    assert r["live_workers"] == 1 and r["healed_after_s"] is not None and r["healed_after_s"] <= 10
    assert r["calls_still_reserved_at_end"] == 0 and r["borrowers_with_two_live_calls"] == 0
    assert not r["invariant_violations"] and r["calls_started_after_crash"] > 20      # the survivor keeps dialing


def test_worker_crash_before_provider_call():
    r = drills.worker_crash("after_reserve")
    assert r["healed_after_s"] is not None and r["healed_after_s"] <= 10
    assert r["reconciler_released"] >= 1 and r["fate_of_orphaned_calls"] == {"FAILED/PLACEMENT_FAILED": 1}
    assert not r["invariant_violations"] and r["calls_still_reserved_at_end"] == 0


def test_one_provider_down_traffic_shifts_to_the_other():
    r = drills.provider_outage(both=False)
    assert r["breaker_opened_after_s"] is not None and r["breaker_opened_after_s"] < 15
    assert r["placed_during_outage"]["A"] == 0 and r["placed_during_outage"]["B"] > 20
    assert r["provider_A_used_again_after_s"] is not None                              # probes, then closes again
    assert r["utilization_recovered"] > 0.6 and not r["invariant_violations"]
    assert r["abandon_rate_overall"] <= 0.03


def test_all_providers_down_dialing_stops_and_resumes():
    r = drills.provider_outage(both=True)
    assert r["placed_during_outage"] == {"A": 0, "B": 0}
    assert r["decisions_during_outage"].get("REJECT", 0) > 50
    assert r["provider_A_used_again_after_s"] is not None and r["utilization_recovered"] > 0.6
    assert not r["invariant_violations"]


def test_agents_dropping_is_absorbed_within_a_tick_or_two():
    r = drills.agent_drop(40)
    assert r["online_before"] == 100 and r["online_after"] == 60
    assert r["in_flight_peak_after_drop"] <= 0.5 * r["in_flight_before"]
    assert r["abandoned_during_drop"] <= 2 and r["abandon_rate_after"] <= 0.03
    assert "AVAILABILITY_SHOCK:FALLBACK" in r["safety_rules_fired"]
    assert not r["invariant_violations"]


def test_duplicates_and_reordering_do_not_multiply_transitions():
    r = drills.messy_provider()
    assert r["events_by_disposition"].get("DUPLICATE", 0) > 100
    assert r["repeated_transitions"] == 0 and r["max_transitions_on_one_call"] <= 6
    assert r["connect_transitions"] == r["calls_connected"]                           # one CONNECTED per call, ever
    assert r["stuck_connected"] == 0 and not r["invariant_violations"]
    assert r["summary"]["abandon_rate"] <= 0.04


def test_answer_rate_shock_does_not_cause_a_flood_of_abandons():
    r = drills.answer_rate_shock()
    assert r["after_jump_to_70pct"]["abandon_rate"] <= 0.03
    assert r["after_drop_to_10pct"]["utilization"] > 0.4 and not r["invariant_violations"]


def test_three_workers_on_stressed_providers_stay_consistent():
    sim = Simulation(agents=80, mode="PREDICTIVE", scenario="D", workers=3, stressed=True, seed=21)
    sim.run(420)
    assert sim.invariants() == []
    sm = sim.summary(120)
    assert sm["abandon_rate"] <= 0.04
    assert sm["calls_connected"] > 100


def test_each_mode_beats_nothing_and_predictive_beats_progressive_on_a_low_answer_rate():
    res = {}
    for mode in ("PROGRESSIVE", "PREDICTIVE"):
        s = Simulation(agents=100, mode=mode, scenario="A", seed=3)
        s.run(420)
        res[mode] = s.summary(120)
        assert s.invariants() == []
    assert res["PREDICTIVE"]["talk_utilization"] > res["PROGRESSIVE"]["talk_utilization"] + 0.15
    assert res["PREDICTIVE"]["abandon_rate"] <= 0.03
    # progressive can never have more agent-bound calls than agents
    assert res["PROGRESSIVE"]["peak_in_flight"] <= 100


def test_all_open_breakers_recover_after_cooldown():
    from smartdialer.clock import SimClock
    from smartdialer.config import SafetyConfig
    from smartdialer.providers.router import ProviderRouter
    from helpers import ScriptedProvider
    ck, cfg = SimClock(0), SafetyConfig()
    r = ProviderRouter([ScriptedProvider("A")], ck, cfg)
    for _ in range(cfg.breaker_min_calls):
        r.record_failure("A")
    assert r.health()["A"]["state"] == "OPEN"
    ck.advance(cfg.breaker_cooldown_s + 0.1)
    assert r.health()["A"]["state"] == "HALF_OPEN"        # regression: used to stay OPEN forever, so nobody ever probed
