# Simulation results

100 agents, 15 simulated minutes per run (first 2 minutes excluded as warm-up), seed 7, providers normal.
Abandon rate is abandoned / answered, as the safety controller measures it (limit 3%).

| Scenario | Mode | Talk util. | Occupancy | Abandon | Dials/connect | Peak ringing | Violations |
|---|---|---|---|---|---|---|---|
| A | Progressive | 49.4% | 55.4% | 0.00% | 4.83 | 59 | 0 |
| A | Predictive | 74.6% | 83.8% | 0.00% | 4.89 | 111 | 0 |
| B | Progressive | 65.4% | 76.2% | 0.00% | 1.9 | 38 | 0 |
| B | Predictive | 76.2% | 88.7% | 0.15% | 1.92 | 48 | 0 |
| C | Progressive | 83.7% | 90.6% | 0.00% | 1.4 | 18 | 0 |
| C | Predictive | 88.9% | 96.2% | 0.26% | 1.44 | 20 | 0 |
| D | Progressive | 57.7% | 65.8% | 0.25% | 3.14 | 67 | 0 |
| D | Predictive | 65.5% | 75.5% | 1.16% | 3.6 | 154 | 0 |

Regenerate with `python scripts/simulate.py --write docs/SIMULATION_RESULTS.md`.
