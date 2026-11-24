from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


class ProviderError(Exception):
    """Definite failure: the provider did not place the call."""


class ProviderTimeout(ProviderError):
    """Ambiguous: the call may or may not exist at the provider. Never fail over on this."""


@dataclass(frozen=True)
class DialRequest:
    call_id: str           # also the idempotency key at the provider
    phone: str
    campaign_id: str


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    event_id: str          # unique per provider; duplicates share it
    call_id: str
    type: str              # RINGING | ANSWERED | COMPLETED | NO_ANSWER | BUSY | FAILED
    provider_ts: float
    talk_s: float = 0.0    # COMPLETED only: how long the borrower was on the line


@dataclass
class ProviderCallStatus:
    known: bool
    state: str = ""        # RINGING | ANSWERED | ENDED
    talk_s: float = 0.0


@dataclass
class Snapshot:
    """What the Safety Controller sees. Built from the database inside the allocation lock, so it
    is authoritative. The pacing engine gets a (possibly stale) read-only copy of the same shape."""
    ts: float
    online: int                  # agents not OFFLINE/PAUSED
    available: int
    reserved: int
    dialing: int
    connected: int
    wrap_up: int
    wrap_ending_soon: int        # WRAP_UP agents free within the lookahead horizon
    paused: int
    offline: int
    unanswered_bound: int
    unanswered_overflow: int
    held: int                    # answered, waiting for an agent
    eligible_borrowers: int
    campaign_status: str = "RUNNING"

    @property
    def unanswered(self) -> int:
        return self.unanswered_bound + self.unanswered_overflow

    @property
    def in_play(self) -> int:
        """Agents that are working this campaign right now (not paused / offline)."""
        return self.online


@dataclass
class PacingProposal:
    """The only thing a pacing engine can produce: a request. It is not an instruction."""
    mode: str                    # PROGRESSIVE | PREDICTIVE
    want: int                    # new calls it would like to start now
    ts: float
    explain: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleResult:
    rule: str
    effect: str                  # PASS | REDUCE | REJECT | FALLBACK
    detail: str
    limit: Optional[int] = None


@dataclass
class ApprovedDispatch:
    """Only SafetyController can mint one (see safety._MINT). The allocator refuses anything else."""
    campaign_id: str
    verdict: str                 # APPROVE | REDUCE | REJECT | FALLBACK_PROGRESSIVE
    effective_mode: str          # what the controller actually allowed
    total: int
    max_bound: int
    max_overflow: int
    rules: List[RuleResult]
    requested: int
    _seal: object = field(default=None, repr=False, compare=False)
