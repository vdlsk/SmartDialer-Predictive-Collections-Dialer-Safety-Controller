"""Two mock providers that misbehave in different ways, plus a model of the borrowers they call.

Every call's whole timeline is decided when it is placed (ring, answer, talk, hang-up). Delivery is
then degraded per provider: latency, jitter, duplicates, reordering, lost events, timeouts, outages.
All on the injected clock, so the same seed gives the same run.
"""
import heapq
import itertools
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

from ..clock import Clock
from ..models import DialRequest, ProviderError, ProviderTimeout, ProviderEvent, ProviderCallStatus
from .base import TelecomProvider

Num = Union[float, Callable[[float], float]]


def _v(x: Num, t: float) -> float:
    return x(t) if callable(x) else x


class BorrowerModel:
    """Who picks up and how long they talk. Either value may be a function of time (scenario D)."""

    def __init__(self, answer_rate: Num = 0.3, talk_mean_s: Num = 90.0, ring_mean_s: float = 7.0, seed: int = 1):
        self.answer_rate, self.talk_mean_s, self.ring_mean_s = answer_rate, talk_mean_s, ring_mean_s
        self.rng = random.Random(seed)

    def answers(self, now) -> bool:
        return self.rng.random() < _v(self.answer_rate, now)

    def talk(self, now) -> float:
        mean = _v(self.talk_mean_s, now)
        return max(8.0, self.rng.gammavariate(4.0, mean / 4.0))     # gamma(4): realistic, not memoryless

    def ring(self) -> float:
        return 2.0 + self.rng.expovariate(1.0 / max(0.5, self.ring_mean_s - 2.0))


@dataclass
class MockProfile:
    setup_s: tuple = (0.3, 0.8)          # time until the first ring
    net_s: tuple = (0.1, 0.3)            # event delivery latency
    reject_rate: float = 0.005           # definite failure at place_call
    timeout_rate: float = 0.0            # ambiguous failure at place_call
    ghost_prob: float = 0.5              # a timed-out call that was actually placed
    carrier_fail_rate: float = 0.005     # FAILED event after placement
    dup_rate: float = 0.0
    reorder_rate: float = 0.0
    reorder_delay_s: tuple = (2.0, 15.0)
    drop_rate: float = 0.0
    no_answer_ring_s: float = 28.0
    busy_share: float = 0.1


@dataclass
class _Call:
    call_id: str
    placed_at: float
    ring_at: float
    answer_at: Optional[float]
    end_at: float
    end_type: str
    talk: float
    cancelled_at: Optional[float] = None
    failed: bool = False


class MockProvider(TelecomProvider):
    def __init__(self, name: str, clock: Clock, model: BorrowerModel, profile: MockProfile, seed: int = 7):
        self.name, self.clock, self.model, self.p = name, clock, model, profile
        self.rng = random.Random(seed)
        self.calls: Dict[str, _Call] = {}
        self._q: list = []
        self._seq = itertools.count()
        self._eid = itertools.count(1)
        self.outage = False
        self.stats = dict(placed=0, rejected=0, timeouts=0, ghosts=0, dups=0, reordered=0, dropped=0, events=0)

    # ------------------------------------------------------------ fault injection (runtime)
    def set_outage(self, on: bool):
        self.outage = on

    # ------------------------------------------------------------ TelecomProvider
    def place_call(self, req: DialRequest) -> None:
        now = self.clock.now()
        if req.call_id in self.calls:
            return                                             # idempotent
        if self.outage:
            self.stats["timeouts"] += 1
            raise ProviderTimeout(f"{self.name}: no response")
        if self.rng.random() < self.p.reject_rate:
            self.stats["rejected"] += 1
            raise ProviderError(f"{self.name}: 503")
        timed_out = self.rng.random() < self.p.timeout_rate
        if timed_out and self.rng.random() >= self.p.ghost_prob:
            self.stats["timeouts"] += 1
            raise ProviderTimeout(f"{self.name}: timeout")     # really not placed
        self._register(req, now)
        if timed_out:                                          # placed, but the caller never heard
            self.stats["timeouts"] += 1
            self.stats["ghosts"] += 1
            raise ProviderTimeout(f"{self.name}: timeout (call exists)")

    def _register(self, req: DialRequest, now: float):
        p, m = self.p, self.model
        ring_at = now + self.rng.uniform(*p.setup_s)
        self.stats["placed"] += 1
        if self.rng.random() < p.carrier_fail_rate:
            c = _Call(req.call_id, now, ring_at, None, ring_at + 1.0, "FAILED", 0.0, failed=True)
            self.calls[req.call_id] = c
            self._emit(c, ring_at + 1.0, "FAILED")
            return
        if m.answers(now):
            answer_at = ring_at + m.ring()
            talk = m.talk(now)
            c = _Call(req.call_id, now, ring_at, answer_at, answer_at + talk, "COMPLETED", talk)
            self.calls[req.call_id] = c
            self._emit(c, ring_at, "RINGING")
            self._emit(c, answer_at, "ANSWERED")
            self._emit(c, answer_at + talk, "COMPLETED", talk)
        else:
            busy = self.rng.random() < p.busy_share
            end = ring_at + (3.0 if busy else p.no_answer_ring_s)
            c = _Call(req.call_id, now, ring_at, None, end, "BUSY" if busy else "NO_ANSWER", 0.0)
            self.calls[req.call_id] = c
            self._emit(c, ring_at, "RINGING")
            self._emit(c, end, c.end_type)

    def _emit(self, c: _Call, at: float, typ: str, talk: float = 0.0):
        p = self.p
        if self.rng.random() < p.drop_rate:
            self.stats["dropped"] += 1
            return
        deliver = at + self.rng.uniform(*p.net_s)
        if self.rng.random() < p.reorder_rate:
            deliver += self.rng.uniform(*p.reorder_delay_s)
            self.stats["reordered"] += 1
        eid = f"{self.name}-{next(self._eid)}"
        ev = ProviderEvent(self.name, eid, c.call_id, typ, at, talk)
        self._push(deliver, ev, c)
        if self.rng.random() < p.dup_rate:
            self.stats["dups"] += 1
            self._push(deliver + self.rng.uniform(0.0, 3.0), ev, c)                        # same event_id, redelivered
            if self.rng.random() < 0.5:                                                    # same fact, new event_id
                ev2 = ProviderEvent(self.name, f"{self.name}-{next(self._eid)}", c.call_id, typ, at, talk)
                self._push(deliver + self.rng.uniform(0.0, 5.0), ev2, c)

    def _push(self, deliver_at, ev, c):
        heapq.heappush(self._q, (deliver_at, next(self._seq), ev, c))

    def cancel_call(self, call_id: str) -> None:
        c = self.calls.get(call_id)
        now = self.clock.now()
        if self.outage:
            raise ProviderTimeout(f"{self.name}: no response")
        if c is None or c.cancelled_at is not None or now >= c.end_at:
            return
        c.cancelled_at = now
        if c.answer_at is not None and now >= c.answer_at:        # hangup on a live call
            self._emit(c, now, "COMPLETED", now - c.answer_at)
        # drop everything still scheduled in the future for this call
        self._q = [e for e in self._q if not (e[3] is c and e[2].provider_ts > now)]
        heapq.heapify(self._q)

    def get_call_status(self, call_id: str) -> ProviderCallStatus:
        if self.outage:
            raise ProviderTimeout(f"{self.name}: no response")
        c = self.calls.get(call_id)
        now = self.clock.now()
        if c is None:
            return ProviderCallStatus(False)
        if c.cancelled_at is not None or now >= c.end_at:
            talk = 0.0
            if c.answer_at is not None:
                talk = (min(now, c.cancelled_at or now, c.end_at)) - c.answer_at
            return ProviderCallStatus(True, "ENDED", max(0.0, talk))
        if c.answer_at is not None and now >= c.answer_at:
            return ProviderCallStatus(True, "ANSWERED", now - c.answer_at)
        return ProviderCallStatus(True, "RINGING")

    def poll_events(self) -> List[ProviderEvent]:
        if self.outage:
            return []                                           # held at the provider until it recovers
        now, out = self.clock.now(), []
        while self._q and self._q[0][0] <= now:
            out.append(heapq.heappop(self._q)[2])
        self.stats["events"] += len(out)
        return out


def provider_a(clock, model, seed=11) -> MockProvider:
    """Fast, reliable, boring."""
    return MockProvider("A", clock, model, MockProfile(setup_s=(0.3, 0.8), net_s=(0.1, 0.3), reject_rate=0.003,
                                                        carrier_fail_rate=0.004), seed)


def provider_b(clock, model, seed=13) -> MockProvider:
    """Slower, times out, duplicates events, delivers out of order, loses a few."""
    return MockProvider("B", clock, model, MockProfile(setup_s=(1.0, 3.0), net_s=(0.5, 2.5), reject_rate=0.01,
                                                        timeout_rate=0.04, ghost_prob=0.5, carrier_fail_rate=0.01,
                                                        dup_rate=0.12, reorder_rate=0.08, drop_rate=0.004), seed)
