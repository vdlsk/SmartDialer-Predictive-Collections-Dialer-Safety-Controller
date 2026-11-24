"""Scenario definitions: how borrowers behave, and how bad the telecom network is."""
from dataclasses import dataclass, field
from typing import Callable, Dict, Union

Num = Union[float, Callable[[float], float]]


def _ramp(points):
    """Piecewise-linear function of simulated seconds-since-start from [(t, value), ...]."""
    def f(t):
        if t <= points[0][0]:
            return points[0][1]
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if t <= t1:
                return v0 + (v1 - v0) * (t - t0) / max(1e-9, t1 - t0)
        return points[-1][1]
    return f


@dataclass
class Scenario:
    key: str
    title: str
    answer_rate: Union[float, list]       # float, or list of (t, value) ramp points
    talk_mean_s: Union[float, list]
    note: str = ""

    def build(self, t0: float):
        def wrap(x):
            if isinstance(x, list):
                f = _ramp(x)
                return lambda now: f(now - t0)
            return x
        return wrap(self.answer_rate), wrap(self.talk_mean_s)


SCENARIOS: Dict[str, Scenario] = {
    "A": Scenario("A", "Low answer rate", 0.20, 120.0, "20% answer, 120 s talk"),
    "B": Scenario("B", "Medium", 0.50, 90.0, "50% answer, 90 s talk"),
    "C": Scenario("C", "High answer, long calls", 0.70, 180.0, "70% answer, 180 s talk"),
    "D": Scenario("D", "Changing", [(0, 0.55), (120, 0.55), (150, 0.12), (300, 0.12), (330, 0.70), (480, 0.70), (510, 0.30)],
                  [(0, 90), (200, 90), (260, 180), (400, 180), (460, 70)],
                  "answer rate 55% -> 12% -> 70% -> 30%, talk time 90 -> 180 -> 70 s"),
}


def stress(profile):
    """Degrade a MockProfile: slower, flakier, noisier."""
    profile.setup_s = (profile.setup_s[0] * 2.5, profile.setup_s[1] * 3.0)
    profile.net_s = (profile.net_s[0] * 3.0, profile.net_s[1] * 3.0)
    profile.reject_rate = max(profile.reject_rate, 0.06)
    profile.timeout_rate = max(profile.timeout_rate, 0.06)
    profile.carrier_fail_rate = max(profile.carrier_fail_rate, 0.03)
    profile.dup_rate = max(profile.dup_rate, 0.10)
    profile.reorder_rate = max(profile.reorder_rate, 0.08)
    return profile
