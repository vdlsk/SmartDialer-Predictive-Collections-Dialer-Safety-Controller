"""Picks a provider per call and keeps a circuit breaker per provider.

Failover rule worth stating out loud: we fail over to another provider only after a DEFINITE
rejection. After a timeout we don't know whether the first provider placed the call, so we retry the
same provider with the same idempotency key and, if that also times out, give up on that call (a
cancel is queued) instead of risking two simultaneous calls to one borrower.
"""
import random
from collections import deque
from typing import Dict, List, Optional

from ..clock import Clock
from ..config import SafetyConfig
from ..models import DialRequest, ProviderError, ProviderTimeout
from .base import TelecomProvider


class Breaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, cfg: SafetyConfig):
        self.cfg, self.state, self.opened_at = cfg, self.CLOSED, 0.0
        self.win: deque = deque()
        self.probes = 0

    def _trim(self, now):
        while self.win and self.win[0][0] < now - self.cfg.breaker_window_s:
            self.win.popleft()

    def allow(self, now) -> bool:
        if self.state == self.OPEN:
            if now - self.opened_at >= self.cfg.breaker_cooldown_s:
                self.state, self.probes = self.HALF_OPEN, 0
            else:
                return False
        if self.state == self.HALF_OPEN:
            return self.probes < 3
        return True

    def record(self, ok: bool, now):
        if self.state == self.HALF_OPEN:
            self.probes += 1
            if not ok:
                self.state, self.opened_at = self.OPEN, now
            elif self.probes >= 3:
                self.state = self.CLOSED
                self.win.clear()
            return
        self.win.append((now, ok))
        self._trim(now)
        n = len(self.win)
        if self.state == self.CLOSED and n >= self.cfg.breaker_min_calls:
            fails = sum(1 for _, o in self.win if not o)
            if fails / n >= self.cfg.breaker_fail_threshold:
                self.state, self.opened_at = self.OPEN, now

    def effective_state(self, now) -> str:
        """Report OPEN breakers as HALF_OPEN once the cooldown has passed. Without this a campaign whose
        every breaker is open would never place the probe call that could close one."""
        if self.state == self.OPEN and now - self.opened_at >= self.cfg.breaker_cooldown_s:
            self.state, self.probes = self.HALF_OPEN, 0
        return self.state

    def failure_rate(self, now) -> float:
        self._trim(now)
        return (sum(1 for _, o in self.win if not o) / len(self.win)) if self.win else 0.0


class ProviderRouter:
    def __init__(self, providers: List[TelecomProvider], clock: Clock, cfg: SafetyConfig,
                 weights: Optional[Dict[str, float]] = None, seed: int = 3):
        self.providers = {p.name: p for p in providers}
        self.clock, self.cfg = clock, cfg
        self.breakers = {p.name: Breaker(cfg) for p in providers}
        self.weights = weights or {p.name: 1.0 for p in providers}
        self.rng = random.Random(seed)

    def _order(self) -> List[str]:
        now = self.clock.now()
        ok = [n for n in self.providers if self.breakers[n].allow(now)]
        out = []
        while ok:
            w = [self.weights.get(n, 1.0) for n in ok]
            pick = self.rng.choices(ok, w)[0]
            out.append(pick)
            ok.remove(pick)
        return out

    def any_available(self) -> bool:
        now = self.clock.now()
        return any(b.state != Breaker.OPEN or now - b.opened_at >= self.cfg.breaker_cooldown_s for b in self.breakers.values())

    def health(self) -> Dict[str, dict]:
        now = self.clock.now()
        return {n: {"state": b.effective_state(now), "failure_rate": round(b.failure_rate(now), 3)} for n, b in self.breakers.items()}

    def record_failure(self, provider: str):
        if provider in self.breakers:
            self.breakers[provider].record(False, self.clock.now())

    def place(self, req: DialRequest, retries: int = 1) -> str:
        """Returns the provider name that accepted. Raises ProviderError (definite) or ProviderTimeout."""
        last: Optional[Exception] = None
        for name in self._order():
            prov, br = self.providers[name], self.breakers[name]
            for attempt in range(retries + 1):
                try:
                    prov.place_call(req)
                    br.record(True, self.clock.now())
                    return name
                except ProviderTimeout as e:
                    br.record(False, self.clock.now())
                    last = e
                    if not br.allow(self.clock.now()):
                        break
                    continue                      # retry same provider, same key
                except ProviderError as e:
                    br.record(False, self.clock.now())
                    last = e
                    break                         # definite: safe to try the next provider
            if isinstance(last, ProviderTimeout):
                last.provider = name
                raise last                        # ambiguous: stop, never double-dial
        if last is None:
            raise ProviderError("no provider available (all breakers open)")
        raise last

    def find_call(self, call_id: str):
        """Which provider (if any) has this call? Raises ProviderTimeout if nobody has it and at least
        one provider could not be asked, because then absence proves nothing."""
        unreachable = False
        for name, p in self.providers.items():
            try:
                st = p.get_call_status(call_id)
            except ProviderError:
                unreachable = True
                continue
            if st.known:
                return name, st
        if unreachable:
            raise ProviderTimeout("cannot rule out the call: a provider is unreachable")
        return None

    def cancel(self, provider: str, call_id: str):
        self.providers[provider].cancel_call(call_id)

    def status(self, provider: str, call_id: str):
        return self.providers[provider].get_call_status(call_id)

    def poll_all(self):
        evs = []
        for p in self.providers.values():
            evs.extend(p.poll_events())
        return evs
