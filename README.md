# SmartDialer

A collections dialer with two pacing modes, progressive and predictive, and a separate Safety Controller that sits between pacing and the telecom provider. It runs against two mock providers, on a simulated clock, with a live dashboard.

Python 3.10+ and the standard library only (SQLite for state). `pytest` is the only extra, for the tests.

## Run it

```bash
python -m smartdialer.server          # dashboard at http://127.0.0.1:8765
python -m pytest -q                   # 65 tests, about 40 s
python scripts/simulate.py            # scenarios A-D, progressive vs predictive
python scripts/failure_drills.py      # the five failure cases, plus an answer-rate shock
python scripts/loadtest.py            # concurrent reservations and events
python scripts/scale_sweep.py         # per-tick cost at 100 / 1,000 / 10,000 agents
```

Options for the server: `--port 8765 --agents 100 --workers 2 --mode PREDICTIVE --scenario B --speed 10`.

## The dashboard

Press play (or space). Everything is live and nothing needs a reload.

- **Borrowers.** Pick scenario A, B, C or D, or drag the answer-rate and talk-time sliders. D changes by itself.
- **Agents.** Lose 10 or 40 at once, bring them back, add 20.
- **Providers.** Take A or B offline, degrade both, or switch on a duplicate and out-of-order storm for B.
- **Workers.** Crash a worker at either dangerous point (after reserving, or after the provider accepted the call), then start a replacement.
- **Why it dialed.** Every pacing decision with the safety controller's verdict. Click one to see each rule it ran and which ones cut the request.
- **Agent floor.** One square per agent; a dot means that agent has a call ringing for them. Hover for the agent id.

The banner at the top turns red if an invariant is ever broken (it should not).

## Where things are

| Path | What it is |
|---|---|
| `smartdialer/pacing/` | Progressive and predictive engines, binomial tail maths, answer-rate estimator. Imports nothing from the safety, database or provider code. |
| `smartdialer/safety.py` | Safety Controller: rules, abandon budget, shedding, sealed approvals |
| `smartdialer/allocator.py` | Reserves agents and borrowers atomically, then places calls |
| `smartdialer/calls.py`, `agents.py`, `states.py` | Call and agent state machines, event handling |
| `smartdialer/providers/` | Provider interface, two mock providers, router with circuit breaker |
| `smartdialer/housekeeping.py` | Reconciler for stuck or orphaned calls |
| `smartdialer/sim/` | Simulated clock, borrower model, harness, invariant checker, failure drills |
| `tests/` | Unit, concurrency, recovery and architecture tests |
| `docs/` | ARCHITECTURE.md (ADRs and written answers), diagrams, measured results |

## Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design decisions, race handling, predictive maths, scale analysis, the final question, what I am least sure of
- [docs/architecture.svg](docs/architecture.svg), [docs/agent_state_machine.svg](docs/agent_state_machine.svg), [docs/call_state_machine.svg](docs/call_state_machine.svg)
- [docs/SIMULATION_RESULTS.md](docs/SIMULATION_RESULTS.md), [docs/FAILURE_DRILLS.md](docs/FAILURE_DRILLS.md), [docs/LOADTEST_RESULTS.txt](docs/LOADTEST_RESULTS.txt), [docs/SCALE_RESULTS.txt](docs/SCALE_RESULTS.txt)

## Assumptions

- No real telephony. The two providers are mocks with latency, failures, duplicates and reordering. A real adapter implements `TelecomProvider` and posts its webhooks to `POST /webhook/<provider>`.
- The simulator runs on a virtual clock, so a 15-minute scenario takes a few seconds.
- SQLite stands in for Postgres. Every state change is a conditional UPDATE checked by row count, which carries over to Postgres unchanged.
- A call is "abandoned" when the borrower answered and no agent was connected within 2 seconds. The 3% limit is measured over a rolling 5 minutes.
