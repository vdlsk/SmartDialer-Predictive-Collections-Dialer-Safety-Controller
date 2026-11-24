#!/usr/bin/env python3
"""What does one dialer tick cost at 100 / 1,000 / 10,000 agents, and where does the time go?

    python scripts/scale_sweep.py                 # 100 and 1000 agents (about 40 s)
    python scripts/scale_sweep.py --agents 100 1000 10000

A tick is budgeted at 500 ms of campaign time. If the work for one tick takes longer than that in
wall-clock time the dialer falls behind real time, and everything it reasons about gets staler.
"""
import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smartdialer import store, worker as worker_mod
from smartdialer.allocator import CallAllocator
from smartdialer.calls import CallService
from smartdialer.housekeeping import Reconciler
from smartdialer.config import SafetyConfig
from smartdialer.safety import SafetyController
from smartdialer.sim.harness import Simulation
from smartdialer.worker import DialerWorker

T = defaultdict(float)
N = defaultdict(int)


def timed(obj, name, label):
    f = getattr(obj, name)

    def w(*a, **k):
        t = time.perf_counter()
        try:
            return f(*a, **k)
        finally:
            T[label] += time.perf_counter() - t
            N[label] += 1
    setattr(obj, name, w)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def run(agents, warm, measure):
    T.clear(); N.clear()
    # the provider calls-per-second ceiling must grow with the floor: ~agents/105s/answer_rate dials per second
    safety = SafetyConfig(max_cps=max(60.0, agents * 0.05), max_burst=max(400, agents // 2))
    sim = Simulation(agents=agents, mode="PREDICTIVE", scenario="B", workers=1, borrowers=max(20000, agents * 25), seed=2, sample_s=5.0, safety=safety)
    sim.run(warm)
    w = sim.workers[0]
    timed(w, "tick", "tick")
    timed(w, "dispatch", "  dispatch (pacing+review+allocate+place)")
    timed(w, "shed", "  shed")
    timed(w.recon, "run", "  reconciler sweep")
    timed(w.recon, "send_commands", "  outbox")
    timed(w.agents, "release_wrapups", "  wrap-up release")
    timed(w.calls, "expire_holds", "  expire holds")
    timed(w.safety, "review", "    safety review")
    timed(w.allocator, "allocate", "    allocate (reserve agents+borrowers)")
    timed(w.allocator, "place", "    provider placement")
    timed(w.calls, "handle_event", "events (per event)")
    store_snap = store.take_snapshot
    store.take_snapshot = lambda *a, **k: _t(store_snap, "    snapshot (GROUP BY over agents)", *a, **k)
    ticks = []
    t_end = sim.t + measure
    wall0 = time.perf_counter()
    while sim.t < t_end:
        t = time.perf_counter()
        sim.step()
        ticks.append(time.perf_counter() - t)
    wall = time.perf_counter() - wall0
    store.take_snapshot = store_snap
    s = sim.summary(warm)
    return dict(agents=agents, ticks=len(ticks), mean_ms=1000 * sum(ticks) / len(ticks), p99_ms=1000 * pct(ticks, .99),
                realtime_x=measure / wall, util=s["talk_utilization"], abandon=s["abandon_rate"], T=dict(T), N=dict(N),
                events=N["events (per event)"], connected=s["calls_connected"], inv=sim.invariants())


def _t(f, label, *a, **k):
    t = time.perf_counter()
    try:
        return f(*a, **k)
    finally:
        T[label] += time.perf_counter() - t
        N[label] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, nargs="+", default=[100, 1000])
    ap.add_argument("--warm", type=float, default=90)
    ap.add_argument("--measure", type=float, default=30)
    a = ap.parse_args()
    for n in a.agents:
        r = run(n, a.warm, a.measure)
        print(f"\n=== {n} agents: tick mean {r['mean_ms']:.1f} ms, p99 {r['p99_ms']:.1f} ms (budget 500 ms), "
              f"runs {r['realtime_x']:.1f}x real time, util {r['util']:.0%}, abandon {r['abandon']:.1%}, invariants {'OK' if not r['inv'] else r['inv']}")
        ticks = r["ticks"]
        print(f"{'component':<44}{'ms / tick':>10}{'calls / tick':>14}")
        for k, v in sorted(r["T"].items(), key=lambda kv: (not kv[0].startswith("tick"), -kv[1])):
            print(f"{k:<44}{1000 * v / ticks:>10.2f}{r['N'][k] / ticks:>14.1f}")


if __name__ == "__main__":
    main()
