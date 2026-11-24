# Architecture and decisions

Diagrams: [architecture](architecture.svg), [agent states](agent_state_machine.svg), [call states](call_state_machine.svg).

## The shape of it

```
pacing engine --proposal--> Safety Controller --ApprovedDispatch--> allocator --after commit--> provider router --> providers
     |                              |                                   |                              ^
 statistics                  safety state (DB)                  agents, borrowers, calls (DB)      events (webhook)
```

The pacing engine only says how many calls it would like. The Safety Controller decides how many it gets. Only the controller can create an `ApprovedDispatch`, and the allocator refuses anything else. All workers coordinate through the database; none holds state the others need.

## Decisions

**ADR 1. Safety is a separate component, not a rule inside the pacer.**
The pacer is allowed to be wrong, because it works from estimates. The controller works from facts (calls already abandoned, agents actually free, provider health) and never sees the pacer's model. The pacer cannot import the controller, the database or a provider; `tests/test_architecture.py` checks that on the source. Cost: two components to read, and the pacer cannot "just" ask for a special case.

**ADR 2. Every state change is a compare-and-swap.**
`UPDATE agents SET state='RESERVED', call_id=? WHERE id=? AND state='AVAILABLE'`, then check the row count. If it is 0, someone else got there first and we pick another agent. No in-process locks, so it works across workers. Cost: callers must handle losing; there is no blocking.

**ADR 3. Late binding for predictive calls.**
A predictive call is dialed without an agent. When the borrower answers, we take a free agent then. If none is free, the answered call waits up to 2 s; after that it is ABANDONED and counted. Progressive calls and fallback calls are bound to an agent at dial time, so they can never abandon. Cost: the answered-call path needs its own timer and its own tests.

**ADR 4. Place the call after the transaction commits.**
Allocation (agents, borrowers, call rows, safety review) is one write transaction. The provider request happens after it, so no database lock is held across a network call. If we crash in between, the call row is RESERVED with no provider call; the reconciler finds it. Cost: a crash window that has to be reconciled, which is what ADR 5 is for.

**ADR 5. A reconciler repairs anything stuck.**
It looks for calls in RESERVED, INITIATED, RINGING or CONNECTED past their deadline and asks the provider what actually happened (`find_call`). Provider has it: adopt it. Provider does not: release the agent and borrower. Cost: a periodic sweep; at 10,000 agents it is about 19 ms per tick.

**ADR 6. Events are at-least-once and unordered; we make them idempotent.**
Each event is stored under a primary key of (provider, event id) so a repeat is dropped. A call state only moves to a higher rank, so a late RINGING after ANSWERED is ignored. Terminal states never change. An ANSWERED that arrives after we marked the call FAILED or CANCELLED is a ghost: we record it and queue a HANGUP. One transaction per event.

**ADR 7. Fail over only on a definite error.**
If a provider rejects a call or is unreachable, try the other one. If it times out, we do not know whether the call was placed, so we retry the same provider with the same idempotency key rather than risk two calls to one borrower. Each provider has a circuit breaker (closed, open, half-open).

**ADR 8. Python and SQLite, simulated clock.**
So a reviewer can run everything with one command, and so a 15-minute scenario is repeatable. Cost: SQLite has one writer, which is the first thing to break at scale (see below).

## The agent-reservation race

Two workers both see agent 17 as free. Both run the conditional UPDATE above. SQLite serialises the writes; the first changes the row, the second matches 0 rows and moves on to the next agent. Releasing is fenced the same way: `release_agent` only works if `agents.call_id` is still the call doing the release, so a late event for an old call cannot free an agent who is already on a new one. Borrowers get the same treatment through `claimed_by_call`. `tests/test_agent_allocation.py` runs many threads against few agents and checks no agent or borrower is ever held twice; this test caught a real bug during development.

## How predictive pacing decides ("why 17 calls?")

1. **Estimate the answer rate.** Two running estimates (fast and slow), the fast one shrunk toward the slow one, plus the last 20 s of data for fast drops. The pacer plans with the upper end of a Wilson confidence interval, because underestimating the answer rate is what causes abandons.
2. **Count capacity C.** Agents free now, plus agents finishing wrap-up within the horizon, plus a discounted share of agents whose calls are about to end, minus answered calls already waiting.
3. **Choose the number of calls.** The largest T such that the chance of more than C of T calls being answered is at most 5% (binomial tail, using the planning answer rate). Subtract calls already ringing.
4. **Drip-feed.** Release at most 10% of capacity per tick so one bad estimate cannot send a large batch.

The dashboard shows this as a sentence for each decision, for example: "22 agent slots (14 free + 8 freeing within 10 s). Answer rate 48% (planning with 54%). 32 calls may be in flight at no more than 5% risk of more answers than agents; 29 already are."

## What the Safety Controller checks

In order: stale proposal; campaign state; provider health; sudden loss of agents (shock); progressive-only guard (cold start, shock, abandon trip, with a taper back); agent coverage; overflow risk from its own independent model; hard ceiling on calls per free agent; calls-per-second token bucket; burst cap. It can approve, reduce or refuse, and records each rule's verdict in the `decisions` table. When it trips, the campaign runs progressive, and returns to predictive only after the abandon window is healthy again, with hysteresis so it does not flip repeatedly.

## Results

See [SIMULATION_RESULTS.md](SIMULATION_RESULTS.md) and [FAILURE_DRILLS.md](FAILURE_DRILLS.md). In short: predictive raises time on calls by 5 to 25 points over progressive in all four scenarios with abandon below 3%. In every run the invariant checker found no double-booked agent or borrower and no illegal transition.

The measurement caveat: after the answer rate drops suddenly, the estimate takes about a ring timeout (about 28 s) to notice, because calls only report their outcome when they end. That lag is in the safe direction for D's drop (we plan with a rate that is too high for a short while), which is why the shock rule and the abandon guard exist.

## Scale: 100, 1,000, 10,000 agents

Measured per-tick cost in one process (budget 500 ms): 4 ms, 14 ms, 86 ms. Utilisation 72%, 78%, 79%. See [SCALE_RESULTS.txt](SCALE_RESULTS.txt).

| Agents | What breaks first | Fix |
|---|---|---|
| 100 | Nothing. Pacing is statistically noisy at this size (small C means large safety margins), so utilisation is lower than at 1,000. | None needed. |
| 1,000 | The snapshot query, a GROUP BY over all agents on every tick. | Keep running counters updated in the same transaction as each state change, instead of scanning. |
| 10,000 | The single database writer and the per-campaign allocation critical section; event handling (about 31 ms per tick of events); the reconciler's full sweep. | Postgres with row locks (`SELECT ... FOR UPDATE SKIP LOCKED`) for agents and an advisory lock per campaign; incremental counters; reconcile only calls past their deadline, driven by an index or by events. Shard agents by team, accepting that smaller pools lose some of the statistical gain. |

Provider limits (calls per second) and per-provider rate limits will bind before the database does in production.

## Final question

*How would you build a SmartDialer that gets as much of the utilization benefit of predictive dialing as possible, while retaining the deterministic safety characteristics of progressive dialing?*

Keep the two jobs apart. The pacer is as aggressive as it likes and is judged only on utilisation. A controller that cannot be tuned by the pacer judges every request against hard facts: a ceiling on abandoned calls, calls already ringing against agents free, and provider health. The controller can always fall back to progressive, where each call is tied to one agent and cannot abandon, so the worst case is the progressive baseline, not a compliance breach.

To get most of the predictive benefit under that limit: dial only extra calls, never fewer than progressive would; bind them to an agent when the borrower answers, not when dialing; hold an answered call for a short grace period before abandoning it; use a conservative answer-rate estimate and a binomial tail instead of the average; release calls in small steps; and stop predictive pacing the moment the abandon window or an agent-loss shock says the estimate is wrong. In the simulator this gives most of the predictive gain with abandons well under 3%.

## What I am least confident about

Provider circuit-breaker health is kept in each worker's memory, so two workers can disagree for a few seconds about whether a provider is down. Everything else, including the abandon budget and shed state, lives in the database and is shared. The fix is to put breaker state in the same table; I left it out to keep the failover logic simple to read.

## Future work

A real provider adapter behind `/webhook/<provider>`; Postgres; shared breaker state; per-agent skills and call priorities; a hard time-of-day compliance window per borrower.
