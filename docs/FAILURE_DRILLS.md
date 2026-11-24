# Failure drills

Each drill is a scripted run on the simulator; values below are what it measured. `invariant_violations` must be empty: no agent or borrower on two live calls, agent and call records agree, only legal state changes.

## 1. Worker crash after the agent and borrower were reserved (before dialing)

```json
{
 "point": "after_reserve",
 "crash_at_s": 61.0,
 "calls_left_reserved": 1,
 "agents_left_reserved": 1,
 "healed_after_s": 5.5,
 "fate_of_orphaned_calls": {
  "FAILED/PLACEMENT_FAILED": 1
 },
 "reconciler_adopted": 0,
 "reconciler_released": 1,
 "calls_still_reserved_at_end": 0,
 "borrowers_with_two_live_calls": 0,
 "invariant_violations": [],
 "live_workers": 1,
 "calls_started_after_crash": 75
}
```

## 1b. Worker crash after the provider accepted the call (before we recorded it)

```json
{
 "point": "after_place",
 "crash_at_s": 61.0,
 "calls_left_reserved": 1,
 "agents_left_reserved": 1,
 "healed_after_s": 3.0,
 "fate_of_orphaned_calls": {
  "CONNECTED": 1
 },
 "reconciler_adopted": 0,
 "reconciler_released": 0,
 "calls_still_reserved_at_end": 0,
 "borrowers_with_two_live_calls": 0,
 "invariant_violations": [],
 "live_workers": 1,
 "calls_started_after_crash": 74
}
```

## 2. Provider A outage, B stays up

```json
{
 "both_providers": false,
 "in_flight_when_it_started": 39,
 "breaker_opened_after_s": 5.5,
 "placed_during_outage": {
  "A": 0,
  "B": 48
 },
 "decisions_during_outage": {
  "APPROVE": 4,
  "REDUCE": 35,
  "REJECT": 68
 },
 "calls_failed_at_placement": 12,
 "calls_stuck_ringing_gt_40s": 19,
 "cancel_commands_waiting": 11,
 "provider_A_used_again_after_s": 1.0,
 "utilization_before": 0.747,
 "utilization_recovered": 0.767,
 "abandon_rate_overall": 0.0,
 "invariant_violations": [],
 "final_breakers": {
  "A": {
   "state": "CLOSED",
   "failure_rate": 0.0
  },
  "B": {
   "state": "CLOSED",
   "failure_rate": 0.0
  }
 }
}
```

## 2b. Both providers down

```json
{
 "both_providers": true,
 "in_flight_when_it_started": 39,
 "breaker_opened_after_s": 4.5,
 "placed_during_outage": {
  "A": 0,
  "B": 0
 },
 "decisions_during_outage": {
  "APPROVE": 4,
  "REDUCE": 13,
  "REJECT": 103
 },
 "calls_failed_at_placement": 38,
 "calls_stuck_ringing_gt_40s": 39,
 "cancel_commands_waiting": 24,
 "provider_A_used_again_after_s": 1.0,
 "utilization_before": 0.747,
 "utilization_recovered": 0.793,
 "abandon_rate_overall": 0.0,
 "invariant_violations": [],
 "final_breakers": {
  "A": {
   "state": "CLOSED",
   "failure_rate": 0.0
  },
  "B": {
   "state": "CLOSED",
   "failure_rate": 0.0
  }
 }
}
```

## 3. Agents drop from 100 to 60

```json
{
 "online_before": 100,
 "online_after": 60,
 "dropped": 40,
 "in_flight_before": 34,
 "in_flight_peak_after_drop": 8,
 "in_flight_after_10s": 5,
 "in_flight_after_30s": 19,
 "shed_calls": 25,
 "abandoned_during_drop": 0,
 "abandon_rate_after": 0.0,
 "safety_rules_fired": [
  "OVERFLOW_RISK:REDUCE",
  "ABANDON_GUARD:REDUCE",
  "AVAILABILITY_SHOCK:FALLBACK",
  "COLD_START:FALLBACK"
 ],
 "utilization_after": 0.652,
 "invariant_violations": []
}
```

## 4 and 5. Duplicate and out-of-order provider events

```json
{
 "events_by_disposition": {
  "APPLIED": 824,
  "DUPLICATE": 483,
  "STALE": 184,
  "IGNORED_TERMINAL": 135,
  "APPLIED_EARLY": 4
 },
 "repeated_transitions": 0,
 "max_transitions_on_one_call": 6,
 "calls_connected": 191,
 "connect_transitions": 191,
 "completed_before_answered_seen": 4,
 "provider_B": {
  "placed": 400,
  "rejected": 0,
  "timeouts": 0,
  "ghosts": 0,
  "dups": 515,
  "reordered": 377,
  "dropped": 5,
  "events": 1630
 },
 "invariant_violations": [],
 "summary": {
  "talk_utilization": 0.655,
  "calls_connected": 151,
  "abandon_rate": 0.0195
 },
 "stuck_connected": 0
}
```

## 6. Answer rate collapses mid-run (extra)

```json
{
 "utilization_steady_at_70pct": 0.919,
 "after_drop_to_10pct": {
  "utilization": 0.661,
  "estimate_followed_after_s": 63.5
 },
 "after_jump_to_70pct": {
  "abandoned": 0,
  "connected": 100,
  "abandon_rate": 0.0,
  "peak_answered_calls_waiting": 1,
  "planning_rate_reached_50pct_after_s": 63.5,
  "safety_rules_fired": [
   "OVERFLOW_RISK:REDUCE",
   "ABANDON_GUARD:REDUCE",
   "COLD_START:FALLBACK"
  ]
 },
 "invariant_violations": []
}
```
