import dataclasses
import pytest

from helpers import Env
from smartdialer import store
from smartdialer.allocator import UnauthorizedDispatch
from smartdialer.config import PacingConfig, SafetyConfig
from smartdialer.db import bump
from smartdialer.models import ApprovedDispatch, PacingProposal, Snapshot
from smartdialer.pacing.binomial import max_total_calls, tail_gt
from smartdialer.pacing.predictive import PredictivePacing
from smartdialer.pacing.progressive import ProgressivePacing
from smartdialer.pacing.stats import CampaignStats, read_stats
from smartdialer.safety import SafetyController


def snap(**kw):
    base = dict(ts=0.0, online=100, available=10, reserved=0, dialing=0, connected=70, wrap_up=20, wrap_ending_soon=5,
                paused=0, offline=0, unanswered_bound=0, unanswered_overflow=0, held=0, eligible_borrowers=5000)
    base.update(kw)
    return Snapshot(**base)


def stats(p=0.3, n=60):
    return CampaignStats(p, p, min(1, p + 0.08), n, n, 8.0, 14.0, 90.0, n)


# ------------------------------------------------------------------ binomial maths
def test_tail_matches_hand_computed_values():
    assert abs(tail_gt(2, 0.5, 1) - 0.25) < 1e-12
    assert abs(tail_gt(10, 0.3, 3) - 0.3503892816) < 1e-6
    assert tail_gt(5, 0.5, 5) == 0 and tail_gt(5, 0.5, -1) == 1


def test_normal_branch_agrees_with_exact():
    exact = tail_gt(1500, 0.4, 650)           # k > 600 uses normal approx; compare against exact sum
    import math
    lp, lq, lg = math.log(0.4), math.log(0.6), math.lgamma
    cdf = sum(math.exp(lg(1501) - lg(i + 1) - lg(1501 - i) + i * lp + (1500 - i) * lq) for i in range(651))
    assert abs(exact - (1 - cdf)) < 0.01


def test_max_total_calls_is_monotone_and_respects_risk():
    for cap in (1, 5, 20, 100):
        prev = 10 ** 9
        for p in (0.1, 0.2, 0.4, 0.7, 0.95):
            t = max_total_calls(cap, p, 0.05, 5000)
            assert t >= cap and t <= prev                       # higher answer rate -> fewer calls
            assert tail_gt(t, p, cap) <= 0.05 + 1e-9
            if t < 5000:
                assert tail_gt(t + 1, p, cap) > 0.05
            prev = t


def test_bigger_pools_overdial_proportionally_more():
    ratio = lambda cap: max_total_calls(cap, 0.3, 0.05, 100000) / (cap / 0.3)
    assert ratio(5) < ratio(50) < ratio(500) <= 1.0


# ------------------------------------------------------------------ pacing engines
def test_progressive_never_asks_for_more_than_free_agents():
    pe = ProgressivePacing()
    assert pe.propose(snap(available=50), 0).want == 50
    assert pe.propose(snap(available=50, eligible_borrowers=7), 0).want == 7
    assert pe.propose(snap(available=0), 0).want == 0


def test_predictive_explains_itself_and_is_deterministic():
    eng = PredictivePacing(PacingConfig())
    a = eng.propose(snap(), stats(0.3), 0)
    b = eng.propose(snap(), stats(0.3), 0)
    assert a.want == b.want > 0
    for k in ("capacity", "answer_rate_upper", "max_in_flight", "in_flight", "text"):
        assert k in a.explain
    assert str(a.want) in a.explain["text"]


def test_predictive_dials_fewer_when_answer_rate_is_higher():
    eng = PredictivePacing(PacingConfig(drip_fraction=100.0))
    lo = eng.propose(snap(), stats(0.15), 0).want
    hi = eng.propose(snap(), stats(0.80), 0).want
    assert lo > hi >= 0


def test_predictive_counts_calls_in_flight_and_waiting_answers():
    eng = PredictivePacing(PacingConfig(drip_fraction=100.0))
    free = eng.propose(snap(), stats(0.3), 0)
    busy = eng.propose(snap(unanswered_overflow=free.explain["max_in_flight"]), stats(0.3), 0)
    held = eng.propose(snap(held=20), stats(0.3), 0)
    assert busy.want == 0 and held.want < free.want


def test_estimator_reacts_fast_to_an_answer_rate_jump_and_decays_slowly_up(tmp_path):
    env = Env(tmp_path, agents=1)
    cfg = PacingConfig()
    now = 10_000.0
    with env.db.tx() as c:                                       # 10 minutes at 10% answered, 100 dials a minute
        for s in range(0, 570, 5):
            bump(c, env.cid, now - 600 + s, resolved=8, answered=0.8)
    low = read_stats(env.db.conn(), env.cid, now, cfg)
    assert 0.05 < low.p_fast < 0.2
    with env.db.tx() as c:                                       # last 30 seconds: 70% answered
        for s in range(0, 30, 5):
            bump(c, env.cid, now - 30 + s, resolved=8, answered=5.6)
    high = read_stats(env.db.conn(), env.cid, now, cfg)
    assert high.p_upper > 0.55 and high.p_upper > low.p_upper + 0.35    # became cautious within 30 s


# ------------------------------------------------------------------ safety controller
def proposal(want, ts=0.0, mode="PREDICTIVE"):
    return PacingProposal(mode, want, ts, {})


def review(sc, env, want, snp, health=None, now=None, mode="PREDICTIVE", warm=True):
    now = env.clock.now() if now is None else now
    if warm:
        with env.db.tx() as c:
            bump(c, env.cid, now, resolved=200, answered=60, connected=55)
    with env.db.tx() as c:
        return sc.review(c, env.cid, proposal(want, now, mode), snp, health or {"A": {"state": "CLOSED"}}, now)


def test_controller_can_reduce_a_greedy_proposal(tmp_path):
    env = Env(tmp_path, agents=1)
    d = review(SafetyController(SafetyConfig()), env, 10_000, snap(), now=env.clock.now())
    assert d.verdict in ("REDUCE", "REJECT") and d.total < 10_000
    assert d.total <= SafetyConfig().hard_ceiling_ratio * 100
    assert any(r.effect == "REDUCE" for r in d.rules)


def test_controller_rejects_stale_proposals_and_dead_providers(tmp_path):
    env = Env(tmp_path, agents=1)
    sc = SafetyController(SafetyConfig())
    now = env.clock.now()
    with env.db.tx() as c:
        d = sc.review(c, env.cid, proposal(5, now - 60), snap(), {"A": {"state": "CLOSED"}}, now)
    assert d.verdict == "REJECT" and d.total == 0 and any(r.rule == "STALE_PROPOSAL" and r.effect == "REJECT" for r in d.rules)
    d = review(sc, env, 5, snap(), health={"A": {"state": "OPEN"}, "B": {"state": "OPEN"}})
    assert d.verdict == "REJECT" and d.total == 0
    d = review(sc, env, 50, snap(), health={"A": {"state": "HALF_OPEN"}})
    assert d.total <= 3


def test_controller_never_binds_more_calls_to_agents_than_there_are_free_agents(tmp_path):
    env = Env(tmp_path, agents=1)
    sc = SafetyController(SafetyConfig())
    d = review(sc, env, 500, snap(available=7), mode="PROGRESSIVE")
    assert d.max_bound <= 7 and d.max_overflow == 0


def test_cold_start_is_progressive(tmp_path):
    env = Env(tmp_path, agents=1)
    sc = SafetyController(SafetyConfig())
    d = review(sc, env, 80, snap(available=9), warm=False)
    assert d.verdict == "FALLBACK_PROGRESSIVE" and d.max_overflow == 0 and d.max_bound == 9
    assert any(r.rule == "COLD_START" for r in d.rules)


def test_abandon_rate_over_limit_forces_progressive_until_it_recovers(tmp_path):
    env = Env(tmp_path, agents=1)
    sc = SafetyController(SafetyConfig())
    now = env.clock.now()
    with env.db.tx() as c:
        bump(c, env.cid, now, resolved=300, answered=100, connected=90, abandoned=10)     # 10% abandoned
    with env.db.tx() as c:
        d = sc.review(c, env.cid, proposal(60, now), snap(available=4), {"A": {"state": "CLOSED"}}, now)
    assert d.verdict == "FALLBACK_PROGRESSIVE" and d.max_overflow == 0 and d.max_bound == 4
    assert any(r.rule == "ABANDON_GUARD" and r.effect == "FALLBACK" for r in d.rules)
    # stays in fallback through the cooldown even if the rate is momentarily fine
    later = now + 10
    with env.db.tx() as c:
        bump(c, env.cid, later, resolved=300, answered=100, connected=2000)
    with env.db.tx() as c:
        d2 = sc.review(c, env.cid, proposal(60, later), snap(available=4), {"A": {"state": "CLOSED"}}, later)
    assert d2.max_overflow == 0
    much_later = now + SafetyConfig().abandon_cooldown_s + 5
    with env.db.tx() as c:
        d3 = sc.review(c, env.cid, proposal(60, much_later), snap(available=4), {"A": {"state": "CLOSED"}}, much_later)
    assert d3.max_overflow > 0


def test_availability_shock_blocks_new_overflow(tmp_path):
    env = Env(tmp_path, agents=1)
    sc = SafetyController(SafetyConfig())
    now = env.clock.now()
    with env.db.tx() as c:
        bump(c, env.cid, now, resolved=300, answered=90, connected=85)
        sc.review(c, env.cid, proposal(5, now), snap(online=100), {"A": {"state": "CLOSED"}}, now)
    with env.db.tx() as c:
        d = sc.review(c, env.cid, proposal(40, now + 2), snap(online=55, available=10), {"A": {"state": "CLOSED"}}, now + 2)
    assert any(r.rule == "AVAILABILITY_SHOCK" for r in d.rules) and d.max_overflow == 0


def test_rate_limit_token_bucket(tmp_path):
    env = Env(tmp_path, agents=1)
    cfg = SafetyConfig(max_cps=10.0, max_burst=30)
    sc = SafetyController(cfg)
    now = env.clock.now()
    a = review(sc, env, 100, snap(available=100, online=200), mode="PROGRESSIVE", now=now)
    b = review(sc, env, 100, snap(available=100, online=200), mode="PROGRESSIVE", now=now)
    c = review(sc, env, 100, snap(available=100, online=200), mode="PROGRESSIVE", now=now + 1)
    assert a.total == 30 and b.total == 0 and c.total == 10


def test_allocator_refuses_anything_the_controller_did_not_mint(tmp_path):
    env = Env(tmp_path, agents=3)
    forged = ApprovedDispatch(env.cid, "APPROVE", "PREDICTIVE", 100, 0, 100, [], 100)
    with pytest.raises(UnauthorizedDispatch):
        with env.db.tx() as c:
            env.worker.allocator.allocate(c, forged, env.clock.now())
    forged2 = dataclasses.replace(forged, _seal=object())
    with pytest.raises(UnauthorizedDispatch):
        with env.db.tx() as c:
            env.worker.allocator.allocate(c, forged2, env.clock.now())
    assert env.calls_in("RESERVED") == []


def test_stale_request_cannot_exceed_free_agents_in_progressive(tmp_path):
    """The proposal was computed when 40 agents were free; by the time the lock is held only 3 are."""
    env = Env(tmp_path, agents=1)
    sc = SafetyController(SafetyConfig())
    for mode in ("PROGRESSIVE", "PREDICTIVE"):
        d = review(sc, env, 40, snap(available=3, eligible_borrowers=100), mode=mode, warm=(mode == "PREDICTIVE"))
        assert d.total <= 3 or (mode == "PREDICTIVE" and d.max_bound == 0)
        if mode == "PROGRESSIVE" or d.effective_mode == "PROGRESSIVE":
            assert d.total == d.max_bound <= 3 and d.max_overflow == 0
    d = review(sc, env, 40, snap(available=0), mode="PROGRESSIVE")
    assert d.total == 0 and d.max_overflow == 0
