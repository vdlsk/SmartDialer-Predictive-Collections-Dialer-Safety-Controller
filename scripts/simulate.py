"""Run scenarios A-D under progressive and predictive pacing and print a comparison.

    python scripts/simulate.py                 # 100 agents, 15 simulated minutes each
    python scripts/simulate.py --stress        # slower, flakier providers
    python scripts/simulate.py --write docs/SIMULATION_RESULTS.md
"""
import argparse, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from smartdialer.sim.harness import Simulation


def run(mode, scenario, agents, minutes, stressed, seed):
    sim = Simulation(agents=agents, mode=mode, scenario=scenario, workers=2, seed=seed, stressed=stressed)
    sim.run(minutes * 60)
    s = sim.summary()
    s["violations"] = sim.invariants()
    return s


def table(rows):
    head = ["Scenario", "Mode", "Talk util.", "Occupancy", "Abandon", "Dials/connect", "Peak ringing", "Violations"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for sc, mode, s in rows:
        out.append("| %s | %s | %.1f%% | %.1f%% | %.2f%% | %s | %d | %d |" % (
            sc, mode.title(), 100 * s["talk_utilization"], 100 * s["occupancy"], 100 * s["abandon_rate"],
            s["dials_per_connect"], s["peak_in_flight"], len(s["violations"])))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--minutes", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--stress", action="store_true")
    ap.add_argument("--write")
    a = ap.parse_args()
    rows, t0 = [], time.time()
    for sc in "ABCD":
        for mode in ("PROGRESSIVE", "PREDICTIVE"):
            rows.append((sc, mode, run(mode, sc, a.agents, a.minutes, a.stress, a.seed)))
            print("done", sc, mode, file=sys.stderr)
    md = table(rows)
    print(md)
    if a.write:
        with open(a.write, "w") as f:
            f.write("# Simulation results\n\n%d agents, %d simulated minutes per run (first 2 minutes excluded as warm-up), seed %d, providers %s.\n"
                    "Abandon rate is abandoned / answered, as the safety controller measures it (limit 3%%).\n\n%s\n"
                    % (a.agents, a.minutes, a.seed, "degraded" if a.stress else "normal", md))
            f.write("\nRegenerate with `python scripts/simulate.py%s --write docs/SIMULATION_RESULTS.md`.\n" % (" --stress" if a.stress else ""))
    print("(%.0fs wall time)" % (time.time() - t0), file=sys.stderr)


if __name__ == "__main__":
    main()
