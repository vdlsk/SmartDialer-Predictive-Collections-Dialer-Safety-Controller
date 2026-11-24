"""Configuration. SafetyConfig is frozen and only the Safety Controller reads it:
the pacing engine never receives it, so it has no handle to loosen a limit."""
from dataclasses import dataclass, field

BUCKET_S = 5   # metric bucket width (seconds)


@dataclass(frozen=True)
class SafetyConfig:
    max_abandon_rate: float = 0.03        # of answered calls, rolling window (regulatory style 3%)
    abandon_window_s: float = 300.0
    abandon_min_sample: int = 30          # below this the window is too small to trust
    abandon_cooldown_s: float = 60.0      # stay in progressive after tripping
    hold_grace_s: float = 2.0             # answered call may wait this long for an agent
    hard_ceiling_ratio: float = 6.0       # unanswered calls <= ratio * agents in play, always
    max_overflow_fraction: float = 5.0    # unbound unanswered <= this * (agents in play)
    risk_epsilon: float = 0.10            # controller's own tail bound
    credit_horizon_s: float = 8.0         # agents freeing within this many seconds count as capacity (controller's own horizon)
    risk_z: float = 1.28                  # controller's own answer-rate upper bound (z)
    risk_free_haircut: float = 0.35       # credit for live calls ending soon (pacing uses 0.5)
    min_samples_predictive: int = 40      # cold start: progressive until this many outcomes seen
    shock_drop_fraction: float = 0.25     # agents online falling this much ...
    shock_window_s: float = 10.0          # ... within this window = availability shock
    shock_cooldown_s: float = 20.0
    max_cps: float = 60.0                 # new calls per second per campaign (token bucket)
    max_burst: int = 400
    max_staleness_s: float = 5.0          # proposals older than this are rejected
    breaker_fail_threshold: float = 0.5
    breaker_min_calls: int = 8
    breaker_window_s: float = 30.0
    breaker_cooldown_s: float = 15.0


@dataclass
class PacingConfig:
    target_risk: float = 0.05             # P(answers > free agents) the engine aims for
    confidence_z: float = 1.0             # Wilson upper-bound z on answer rate
    prior_answer_rate: float = 0.30
    prior_strength: float = 8.0
    fast_halflife_s: float = 20.0
    slow_halflife_s: float = 120.0
    recent_window_s: float = 20.0         # raw window for the fast attack
    recent_min_n: float = 15.0
    shrink_k: float = 8.0                 # fast window is shrunk toward slow with this many pseudo-calls
    prior_setup_s: float = 12.0
    prior_talk_s: float = 90.0
    future_free_haircut: float = 0.5      # only trust half of "agents about to free up" from live talks
    drip_fraction: float = 0.10           # start at most this share of capacity per tick: bursts answer in bursts
    max_batch: int = 2000


@dataclass
class Timing:
    wrap_up_s: float = 15.0
    reserve_lease_s: float = 5.0          # RESERVED call with no provider ack after this => reconcile
    ring_deadline_s: float = 60.0         # unanswered after this => reconcile / fail
    connected_check_s: float = 300.0      # CONNECTED longer than this => ask the provider if it is still going
    recheck_s: float = 60.0
    max_call_s: float = 1800.0            # CONNECTED longer than this => force close
    place_retries: int = 1                # same-provider, same-idempotency-key retries on timeout
    agent_reservation_s: float = 30.0


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    backoff_s: float = 60.0               # no-answer backoff base (doubles). Real life: hours.
    system_retry_s: float = 5.0
    max_system_failures: int = 5
    abandoned_callback_s: float = 10.0


@dataclass
class CampaignConfig:
    id: str = "c1"
    name: str = "Collections - DPD 30+"
    mode: str = "PREDICTIVE"              # PROGRESSIVE | PREDICTIVE
    timing: Timing = field(default_factory=Timing)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    pacing: PacingConfig = field(default_factory=PacingConfig)
