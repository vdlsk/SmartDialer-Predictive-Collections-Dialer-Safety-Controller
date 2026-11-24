"""Predictive pacing. Pure function of (snapshot, statistics): no database, no provider, no safety
config. Its only output is a PacingProposal, which the Safety Controller may cut or refuse.

The question it answers:  how many calls may be in flight so that the number of borrowers who pick
up is very unlikely to exceed the number of agents who can take them?

    capacity C = agents free now
                 + agents expected to free up before a new call could be answered
                 - answered calls already waiting
    p          = upper-confidence answer rate (pacing/stats.py)
    T_max      = largest T with  P( Binomial(T, p) > C ) <= target_risk
    want       = T_max - unbound calls already in flight (not yet answered)

"Expected to free up" looks one typical setup-time ahead (mean dial-to-answer time): an agent who is
free only after the borrower would already have been waiting is no use to that borrower. Agents finishing
after-call work count in full because their end time is known; agents on live calls count at
`future_free_haircut` of the exponential estimate because talk time is not predictable.
"""
import math

from ..config import PacingConfig
from ..models import PacingProposal, Snapshot
from .binomial import max_total_calls
from .stats import CampaignStats


class PredictivePacing:
    mode = "PREDICTIVE"

    def __init__(self, cfg: PacingConfig):
        self.cfg = cfg

    def horizon(self, stats: CampaignStats) -> float:
        return min(30.0, max(3.0, stats.setup_mean))

    def propose(self, snap: Snapshot, stats: CampaignStats, now: float) -> PacingProposal:
        c = self.cfg
        H = self.horizon(stats)
        p_free = 1.0 - math.exp(-H / max(1.0, stats.talk_mean))
        from_talks = int(math.floor(snap.connected * p_free * c.future_free_haircut))
        from_wrap = snap.wrap_ending_soon
        cap = snap.available + from_talks + from_wrap - snap.held
        cap = max(0, cap)
        in_flight = snap.unanswered_overflow
        p = max(0.01, stats.p_upper)
        limit = int(min(cap / p * 2 + 5, 100000))
        t_max = max_total_calls(cap, p, c.target_risk, limit) if cap > 0 else 0
        gap = max(0, t_max - in_flight)
        drip = max(2, math.ceil(c.drip_fraction * cap))          # smooth: a burst of dials becomes a burst of answers
        want = max(0, min(gap, drip, c.max_batch, snap.eligible_borrowers))
        ex = dict(
            capacity=cap, free_now=snap.available, freeing_talk=from_talks,
            freeing_wrap=from_wrap, held=snap.held, horizon_s=round(H, 1),
            answer_rate=round(stats.p_fast, 3), answer_rate_upper=round(p, 3), samples=round(stats.n_eff, 1),
            talk_mean_s=round(stats.talk_mean, 1), in_flight=in_flight, max_in_flight=t_max,
            target_risk=c.target_risk, eligible=snap.eligible_borrowers,
        )
        ex["text"] = (
            f"Dial {want}: {cap} agent slot{'s' if cap != 1 else ''} "
            f"({snap.available} free + {from_talks + from_wrap} freeing within {H:.0f}s"
            f"{f' - {snap.held} waiting' if snap.held else ''}). "
            f"Answer rate {stats.p_fast:.0%} (planning with {p:.0%}, {stats.n_eff:.0f} samples). "
            f"{t_max} calls may be in flight at <={c.target_risk:.0%} risk of more answers than agents; {in_flight} already are"
            + (f", gap {gap} paced to {want} this tick." if gap > want else ".")
        )
        return PacingProposal("PREDICTIVE", int(want), now, ex)
