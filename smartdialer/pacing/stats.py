"""Campaign statistics for the pacing engine, computed from the shared 5-second buckets.

Answer rate is estimated two ways and combined so the engine is quick to become cautious and slow
to become bold:
  slow  = exponentially-weighted rate (long half-life) shrunk toward a prior
  fast  = short half-life rate, shrunk toward `slow` with k pseudo-calls
and then we take the Wilson UPPER bound of `fast`. Over-estimating the answer rate means dialing
fewer calls, which is the safe direction. If the rate really drops, `fast` follows within a few
dozen calls; if it jumps up, the very first answers push the upper bound up immediately.
"""
import math
from dataclasses import dataclass

from ..config import PacingConfig
from ..config import BUCKET_S


def wilson_upper(p: float, n: float, z: float) -> float:
    if n <= 0:
        return 1.0
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    return min(1.0, (centre + margin) / (1 + z2 / n))


def wilson_lower(p: float, n: float, z: float) -> float:
    if n <= 0:
        return 0.0
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    return max(0.0, (centre - margin) / (1 + z2 / n))


@dataclass
class CampaignStats:
    p_fast: float
    p_slow: float
    p_upper: float          # what the engine plans with
    n_fast: float
    n_eff: float
    setup_mean: float
    setup_p90: float
    talk_mean: float
    samples: float          # decayed count of resolved dials (slow window)
    p_recent: float = 0.0   # raw rate over the last few seconds (fast attack)
    n_recent: float = 0.0


def _decay(age, half):
    return 0.5 ** (age / half)


def read_stats(conn, campaign_id: str, now: float, cfg: PacingConfig) -> CampaignStats:
    since = int((now - 6 * cfg.slow_halflife_s) // BUCKET_S)
    agg = {"fast": {"resolved": 0.0, "answered": 0.0}, "slow": {"resolved": 0.0, "answered": 0.0}}
    su = sq = sn = tsum = tn = 0.0
    rec_res = rec_ans = 0.0
    for r in conn.execute("SELECT bucket, key, val FROM buckets WHERE campaign_id=? AND bucket>=? AND key IN "
                          "('resolved','answered','setup_sum','setup_sq','setup_n','talk_sum','talk_n')",
                          (campaign_id, since)):
        age = max(0.0, now - (r["bucket"] + 0.5) * BUCKET_S)
        k, v = r["key"], r["val"]
        if k in ("resolved", "answered"):
            if age <= cfg.recent_window_s:
                if k == "resolved":
                    rec_res += v
                else:
                    rec_ans += v
            agg["fast"][k] += v * _decay(age, cfg.fast_halflife_s)
            agg["slow"][k] += v * _decay(age, cfg.slow_halflife_s)
        else:
            w = v * _decay(age, cfg.slow_halflife_s)
            if k == "setup_sum": su += w
            elif k == "setup_sq": sq += w
            elif k == "setup_n": sn += w
            elif k == "talk_sum": tsum += w
            elif k == "talk_n": tn += w
    s, f = agg["slow"], agg["fast"]
    p_slow = (s["answered"] + cfg.prior_strength * cfg.prior_answer_rate) / (s["resolved"] + cfg.prior_strength)
    p_fast = (f["answered"] + cfg.shrink_k * p_slow) / (f["resolved"] + cfg.shrink_k)
    n_eff = f["resolved"] + cfg.shrink_k
    p_up = wilson_upper(p_fast, n_eff, cfg.confidence_z)
    p_rec = rec_ans / rec_res if rec_res > 0 else 0.0
    if rec_res >= cfg.recent_min_n:                 # fast attack: a sudden jump is believed after ~15 dials
        p_up = max(p_up, wilson_lower(p_rec, rec_res, cfg.confidence_z))
    pw = 3.0
    setup_mean = (su + pw * cfg.prior_setup_s) / (sn + pw)
    var = max(0.0, (sq + pw * cfg.prior_setup_s ** 2) / (sn + pw) - setup_mean ** 2)
    talk_mean = (tsum + 2.0 * cfg.prior_talk_s) / (tn + 2.0)
    return CampaignStats(p_fast, p_slow, p_up, f["resolved"], n_eff, setup_mean,
                         setup_mean + 1.28 * math.sqrt(var), talk_mean, s["resolved"], p_rec, rec_res)
