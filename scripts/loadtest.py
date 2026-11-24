#!/usr/bin/env python3
"""Load test: how many reservations and provider events per second can N workers push through one
database, and does the allocation protocol stay correct while they do?

    python scripts/loadtest.py                 # 10,000 agents, 1/2/4/8 worker threads
    python scripts/loadtest.py --agents 1000 --threads 1 4

Providers are stubbed to accept instantly, so what is measured is the dialer's own cost: reserving
agents and borrowers, writing call rows, and applying provider events. A real provider adds network
latency per call, but that happens after the transaction has committed and in parallel.
"""
import argparse
import os
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smartdialer import store
from smartdialer.clock import SimClock
from smartdialer.config import CampaignConfig, SafetyConfig
from smartdialer.db import Database
from smartdialer.models import ProviderEvent
from smartdialer.providers.base import TelecomProvider
from smartdialer.providers.router import ProviderRouter
from smartdialer.worker import DialerWorker, bootstrap_campaign


class AcceptAll(TelecomProvider):
    name = "A"

    def place_call(self, req): pass
    def cancel_call(self, call_id): pass
    def get_call_status(self, call_id): raise NotImplementedError
    def poll_events(self): return []


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def run(agents, threads, rounds):
    d = tempfile.mkdtemp(prefix="sd_load_")
    db = Database(os.path.join(d, "load.db"))
    clock = SimClock(10_000.0)
    cfg = CampaignConfig(mode="PROGRESSIVE")
    safety = SafetyConfig(max_cps=1e9, max_burst=10 ** 6)
    bootstrap_campaign(db, cfg, 0)
    with db.tx() as c:
        store.add_borrowers(c, cfg.id, [(f"b{i}", f"+91{i}", i % 100) for i in range(agents * 4)])
        for i in range(agents):
            store.add_agent(c, cfg.id, f"a{i}", "AVAILABLE", 0.0)
    workers = [DialerWorker(f"w{i}", db, clock, ProviderRouter([AcceptAll()], clock, safety), {cfg.id: cfg}, safety, use_lease=False)
               for i in range(threads)]
    lat, errors = [], []
    start = threading.Barrier(threads + 1)

    def work(w):
        start.wait()
        for _ in range(rounds):
            t = time.perf_counter()
            try:
                w.dispatch(cfg.id)
            except Exception as e:                      # pragma: no cover
                errors.append(repr(e))
            lat.append(time.perf_counter() - t)

    ts = [threading.Thread(target=work, args=(w,)) for w in workers]
    [t.start() for t in ts]
    start.wait()
    t0 = time.perf_counter()
    [t.join() for t in ts]
    dt = time.perf_counter() - t0
    c = db.conn()
    calls = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    # ---- correctness after the storm
    dup_agents = c.execute("SELECT COUNT(*) FROM (SELECT agent_id FROM calls WHERE agent_id IS NOT NULL GROUP BY agent_id HAVING COUNT(*)>1)").fetchone()[0]
    dup_borrowers = c.execute("SELECT COUNT(*) FROM (SELECT borrower_id FROM calls GROUP BY borrower_id HAVING COUNT(*)>1)").fetchone()[0]
    over = calls - agents
    # ---- event ingest: three events per call, spread over all workers
    ids = [r[0] for r in c.execute("SELECT id FROM calls")]
    evs = []
    for i, cid in enumerate(ids):
        evs += [ProviderEvent("A", f"{cid}-r", cid, "RINGING", 0), ProviderEvent("A", f"{cid}-a", cid, "ANSWERED", 0),
                ProviderEvent("A", f"{cid}-c", cid, "COMPLETED", 0, 60.0), ProviderEvent("A", f"{cid}-c", cid, "COMPLETED", 0, 60.0)]
    chunks = [evs[i::threads] for i in range(threads)]
    ev_lat = []

    def ingest(w, chunk):
        for ev in chunk:
            t = time.perf_counter()
            w.calls.handle_event(ev)
            ev_lat.append(time.perf_counter() - t)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=ingest, args=(w, ch)) for w, ch in zip(workers, chunks)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ev_dt = time.perf_counter() - t0
    done = c.execute("SELECT COUNT(*) FROM calls WHERE state='COMPLETED'").fetchone()[0]
    # events for one call are spread over threads, so COMPLETED can overtake ANSWERED: a deliberate out-of-order test.
    # Whatever the order, no agent may be left holding a finished call.
    wrap = calls - c.execute("SELECT COUNT(*) FROM agents WHERE state IN ('RESERVED','DIALING','CONNECTED') OR call_id IS NOT NULL").fetchone()[0] + 0
    stuck = c.execute("SELECT COUNT(*) FROM agents WHERE state IN ('RESERVED','DIALING','CONNECTED') OR call_id IS NOT NULL").fetchone()[0]
    wrap = calls if stuck == 0 else -stuck
    return dict(agents=agents, threads=threads, dispatches=len(lat), calls_reserved=calls, reserve_per_s=calls / dt,
                dispatch_p50_ms=pct(lat, .5) * 1000, dispatch_p99_ms=pct(lat, .99) * 1000,
                events=len(evs), events_per_s=len(evs) / ev_dt, event_p50_ms=pct(ev_lat, .5) * 1000, event_p99_ms=pct(ev_lat, .99) * 1000,
                errors=len(errors), double_booked_agents=dup_agents, double_called_borrowers=dup_borrowers, calls_over_agents=max(0, over),
                completed=done, wrap_up=wrap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, nargs="+", default=[10000])
    ap.add_argument("--threads", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--rounds", type=int, default=6)
    a = ap.parse_args()
    print(f"{'agents':>7} {'threads':>7} {'reserve/s':>10} {'disp p50':>9} {'disp p99':>9} {'events/s':>9} {'evt p50':>8} {'evt p99':>8}  correctness")
    for n in a.agents:
        for t in a.threads:
            r = run(n, t, a.rounds)
            ok = r["errors"] == 0 and r["double_booked_agents"] == 0 and r["double_called_borrowers"] == 0 and r["calls_over_agents"] == 0 \
                and r["completed"] == r["calls_reserved"] and r["wrap_up"] == r["calls_reserved"]
            print(f"{n:>7} {t:>7} {r['reserve_per_s']:>10.0f} {r['dispatch_p50_ms']:>8.1f}ms {r['dispatch_p99_ms']:>8.1f}ms "
                  f"{r['events_per_s']:>9.0f} {r['event_p50_ms']:>7.2f}ms {r['event_p99_ms']:>7.2f}ms  "
                  f"{'OK' if ok else 'FAILED ' + str(r)}")


if __name__ == "__main__":
    main()
