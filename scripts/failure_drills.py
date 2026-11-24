"""Run the five failure cases from the brief (plus an answer-rate shock) and write what was measured.

    python scripts/failure_drills.py [--write docs/FAILURE_DRILLS.md]
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from smartdialer.sim import drills

DRILLS = [
    ("1. Worker crash after the agent and borrower were reserved (before dialing)", lambda: drills.worker_crash("after_reserve")),
    ("1b. Worker crash after the provider accepted the call (before we recorded it)", lambda: drills.worker_crash("after_place")),
    ("2. Provider A outage, B stays up", lambda: drills.provider_outage(False)),
    ("2b. Both providers down", lambda: drills.provider_outage(True)),
    ("3. Agents drop from 100 to 60", lambda: drills.agent_drop()),
    ("4 and 5. Duplicate and out-of-order provider events", lambda: drills.messy_provider()),
    ("6. Answer rate collapses mid-run (extra)", lambda: drills.answer_rate_shock()),
]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--write")
    a = ap.parse_args()
    out = ["# Failure drills\n", "Each drill is a scripted run on the simulator; values below are what it measured. "
           "`invariant_violations` must be empty: no agent or borrower on two live calls, agent and call records agree, only legal state changes.\n"]
    for title, fn in DRILLS:
        t = time.time(); r = fn(); r.pop("series", None)
        print("==", title, "(%.0fs)" % (time.time() - t)); print(json.dumps(r, indent=1, default=str))
        out += ["## " + title, "", "```json", json.dumps(r, indent=1, default=str), "```", ""]
    if a.write:
        open(a.write, "w").write("\n".join(out))


if __name__ == "__main__":
    main()
