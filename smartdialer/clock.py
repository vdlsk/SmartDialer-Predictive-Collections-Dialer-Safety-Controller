"""Time source. Every component takes a Clock so the simulator can run on virtual time
and tests can be deterministic. Nothing in the engine calls time.time() directly."""
import time


class Clock:
    def now(self) -> float:
        raise NotImplementedError


class RealClock(Clock):
    def now(self) -> float:
        return time.time()


class SimClock(Clock):
    def __init__(self, start: float = 1_000_000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        self._t += dt
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)
