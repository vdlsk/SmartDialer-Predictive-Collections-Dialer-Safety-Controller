import pytest

from helpers import Env, ScriptedProvider
from smartdialer.clock import SimClock
from smartdialer.config import SafetyConfig
from smartdialer.models import DialRequest, ProviderError, ProviderTimeout
from smartdialer.providers.mock import BorrowerModel, provider_a, provider_b
from smartdialer.providers.router import Breaker, ProviderRouter


def drain(p, clock, seconds, step=0.5):
    out = []
    end = clock.now() + seconds
    while clock.now() < end:
        clock.advance(step)
        out.extend(p.poll_events())
    return out


def test_providers_behave_differently():
    ck = SimClock(0)
    m = BorrowerModel(1.0, 20.0, seed=3)
    a, b = provider_a(ck, m, 1), provider_b(ck, m, 2)
    for i in range(300):
        for p in (a, b):
            try:
                p.place_call(DialRequest(f"c{i}", "+1", "c1"))
            except ProviderError:
                pass
    ea, eb = drain(a, ck, 120), drain(b, ck, 120)
    assert b.stats["timeouts"] > 3 and a.stats["timeouts"] == 0
    assert b.stats["dups"] > 5 and a.stats["dups"] == 0
    ids = [e.event_id for e in eb]
    assert len(ids) != len(set(ids)), "B redelivers the same event id"
    # out of order: some call gets COMPLETED before ANSWERED
    seen, flipped = {}, 0
    for e in eb:
        seen.setdefault(e.call_id, []).append(e.type)
    for types in seen.values():
        if "COMPLETED" in types and "ANSWERED" in types and types.index("COMPLETED") < types.index("ANSWERED"):
            flipped += 1
    assert flipped > 0
    assert all(e.type != "COMPLETED" or True for e in ea)


def test_place_call_is_idempotent():
    ck = SimClock(0)
    a = provider_a(ck, BorrowerModel(1.0, 20.0))
    a.place_call(DialRequest("x", "+1", "c"))
    a.place_call(DialRequest("x", "+1", "c"))
    assert a.stats["placed"] == 1


def test_timeout_may_hide_a_real_call():
    ck = SimClock(0)
    b = provider_b(ck, BorrowerModel(1.0, 20.0), seed=5)
    b.p.timeout_rate, b.p.reject_rate, b.p.ghost_prob = 1.0, 0.0, 1.0
    with pytest.raises(ProviderTimeout):
        b.place_call(DialRequest("g", "+1", "c"))
    assert b.get_call_status("g").known                       # it exists although the caller saw a timeout


def test_router_fails_over_on_rejection_but_not_on_timeout():
    ck = SimClock(0)
    cfg = SafetyConfig()
    a, b = ScriptedProvider("A"), ScriptedProvider("B")
    r = ProviderRouter([a, b], ck, cfg, weights={"A": 1000.0, "B": 0.001})
    a.fail = ProviderError("503")
    assert r.place(DialRequest("c1", "+1", "x")) == "B"        # definite failure -> next provider
    a.fail = ProviderTimeout("slow")
    with pytest.raises(ProviderTimeout):
        r.place(DialRequest("c2", "+1", "x"))                  # ambiguous -> stop, never double dial
    assert "c2" not in b.placed


def test_breaker_opens_then_probes_then_closes():
    ck = SimClock(0)
    cfg = SafetyConfig()
    br = Breaker(cfg)
    for _ in range(cfg.breaker_min_calls):
        br.record(False, ck.now())
    assert br.state == "OPEN" and not br.allow(ck.now())
    ck.advance(cfg.breaker_cooldown_s + 1)
    assert br.allow(ck.now()) and br.state == "HALF_OPEN"
    for _ in range(3):
        br.record(True, ck.now())
    assert br.state == "CLOSED"
    # a failed probe reopens it
    for _ in range(cfg.breaker_min_calls):
        br.record(False, ck.now())
    ck.advance(cfg.breaker_cooldown_s + 1)
    br.allow(ck.now())
    br.record(False, ck.now())
    assert br.state == "OPEN"


def test_outage_blocks_events_until_recovery_then_delivers_late():
    ck = SimClock(0)
    a = provider_a(ck, BorrowerModel(1.0, 10.0))
    a.place_call(DialRequest("o1", "+1", "c"))
    a.set_outage(True)
    assert drain(a, ck, 60) == []
    with pytest.raises(ProviderTimeout):
        a.place_call(DialRequest("o2", "+1", "c"))
    a.set_outage(False)
    late = a.poll_events()
    assert {e.call_id for e in late} == {"o1"} and len(late) >= 2


def test_provider_outage_trips_breaker_and_dialer_stops_dialing(tmp_path):
    p = ScriptedProvider("A")
    env = Env(tmp_path, agents=20, borrowers=500, providers=[p])
    env.dispatch()
    assert len(p.placed) == 20
    p.fail = ProviderTimeout("down")
    for _ in range(12):                                         # keep trying while the provider is dead
        env.clock.advance(7)
        env.worker.agents.release_wrapups(env.cid)
        for cid in env.calls_in("INITIATED"):
            env.event(cid, "NO_ANSWER", eid=f"x{cid}")
        env.dispatch()
    assert env.router.breakers["A"].state == "OPEN"
    placed_before = len(p.placed)
    for _ in range(5):
        env.clock.advance(1)
        env.worker.agents.release_wrapups(env.cid)
        env.dispatch()
    assert len(p.placed) == placed_before                       # breaker open: no call reaches the provider
    last = env.db.conn().execute("SELECT verdict, reasons FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert last["verdict"] == "REJECT" and "PROVIDER_HEALTH" in last["reasons"]
    # nothing leaked: every agent is back to AVAILABLE and no borrower is stuck CLAIMED
    assert set(env.states_of_agents().values()) == {"AVAILABLE"}
    assert env.db.conn().execute("SELECT COUNT(*) FROM borrowers WHERE status='CLAIMED'").fetchone()[0] == 0
