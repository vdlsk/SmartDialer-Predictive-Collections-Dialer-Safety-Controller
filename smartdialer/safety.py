"""Safety Controller: the gate between pacing and the telecom provider.

What makes it a real boundary rather than a suggestion box:
* The pacing engine returns a PacingProposal (a number). Only this module can mint the
  ApprovedDispatch the allocator requires (`_MINT`), and the allocator is the only code holding a
  provider router. There is no flag, argument or config the pacing side can pass to loosen a rule.
* It does not trust the proposal's numbers. It reads authoritative counts (the snapshot is taken
  inside the same write transaction that will create the calls) and its own statistics, and it runs
  its own, more conservative risk model.
* Its limits live in a frozen SafetyConfig that the pacing engine never receives.
* Its state (fallback timers, token bucket, agent-count history) lives in the database, so any
  worker running a review sees the same state and N workers cannot each spend the same budget.

Rules run in order and can only reduce. Each one leaves a line in the decision trace.
"""
import json
import math
from dataclasses import dataclass
from typing import Dict, List

from .config import SafetyConfig, BUCKET_S
from .models import ApprovedDispatch, PacingProposal, RuleResult, Snapshot
from .pacing.binomial import max_total_calls
from .pacing.stats import wilson_upper

_MINT = object()


def is_authentic(d) -> bool:
    return isinstance(d, ApprovedDispatch) and d._seal is _MINT


@dataclass
class Monitor:
    answered_calls: float      # connected + abandoned in window
    abandoned: float
    abandon_rate: float
    resolved_60: float
    answered_60: float
    resolved_300: float
    answered_300: float
    p_upper: float
    samples: float


class SafetyController:
    def __init__(self, cfg: SafetyConfig):
        self._cfg = cfg

    # ---------------------------------------------------------------- independent monitor
    def monitor(self, conn, campaign_id, now) -> Monitor:
        c = self._cfg
        horizon = max(c.abandon_window_s, 300.0)
        since = int((now - horizon) // BUCKET_S)
        w = {"connected": 0.0, "abandoned": 0.0, "r60": 0.0, "a60": 0.0, "r300": 0.0, "a300": 0.0}
        for r in conn.execute("SELECT bucket, key, val FROM buckets WHERE campaign_id=? AND bucket>=? AND key IN "
                              "('connected','abandoned','resolved','answered')", (campaign_id, since)):
            age = now - (r["bucket"] + 0.5) * BUCKET_S
            k, v = r["key"], r["val"]
            if k in ("connected", "abandoned") and age <= c.abandon_window_s:
                w[k] += v
            if k == "resolved":
                w["r300"] += v
                if age <= 60:
                    w["r60"] += v
            if k == "answered":
                w["a300"] += v
                if age <= 60:
                    w["a60"] += v
        answered_calls = w["connected"] + w["abandoned"]
        rate = w["abandoned"] / answered_calls if answered_calls > 0 else 0.0
        p60 = (w["a60"] + 2 * 0.5) / (w["r60"] + 2)                 # weak prior at 0.5: assume answers are likely
        p300 = (w["a300"] + 2 * 0.5) / (w["r300"] + 2)
        p_up = max(wilson_upper(p60, w["r60"] + 2, c.risk_z), p300)
        return Monitor(answered_calls, w["abandoned"], rate, w["r60"], w["a60"], w["r300"], w["a300"], p_up, w["r300"])

    # ---------------------------------------------------------------- persisted controller state
    def _load(self, conn, campaign_id, now) -> Dict:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (f"safety:{campaign_id}",)).fetchone()
        st = json.loads(row["v"]) if row else {}
        st.setdefault("fallback_until", 0.0)
        st.setdefault("shock_until", 0.0)
        st.setdefault("tokens", float(self._cfg.max_burst))
        st.setdefault("tok_ts", now)
        st.setdefault("online", [])
        return st

    def _save(self, conn, campaign_id, st):
        conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (f"safety:{campaign_id}", json.dumps(st)))

    @property
    def horizon_s(self) -> float:
        return self._cfg.credit_horizon_s

    def state(self, conn, campaign_id, now) -> Dict:
        st = self._load(conn, campaign_id, now)
        return {"fallback_until": st["fallback_until"], "shock_until": st["shock_until"]}

    # ---------------------------------------------------------------- shared risk model
    def _risk_cap(self, snap: Snapshot, mon: Monitor) -> int:
        """The controller's own bound on unbound calls in flight (it ignores the pacing engine's number).
        Capacity = free agents + agents finishing wrap-up soon + a haircut share of live calls ending soon
        - answered calls already waiting. Uses its own, more cautious answer-rate bound."""
        c = self._cfg
        free_soon = snap.wrap_ending_soon + int(snap.connected * (1 - math.exp(-c.credit_horizon_s / 90.0)) * c.risk_free_haircut)
        cap = max(0, snap.available + free_soon - snap.held)
        if cap == 0:
            return 0
        p = max(0.02, mon.p_upper)
        return max_total_calls(cap, p, c.risk_epsilon, int(min(cap / p * 2 + 5, 100000)))

    def _guard(self, st: Dict, mon: Monitor, mode: str, now: float):
        """Is overflow (unbound) dialing allowed right now? Returns (reason or None, scale 0..1, name)."""
        c = self._cfg
        if mode == "PROGRESSIVE":
            return "campaign mode is progressive", 0.0, "MODE_GUARD"
        if mon.resolved_300 < c.min_samples_predictive:
            return f"learning: {mon.resolved_300:.0f}/{c.min_samples_predictive} outcomes seen", 0.0, "COLD_START"
        if now < st["shock_until"]:
            return f"availability shock, {st['shock_until'] - now:.0f}s left", 0.0, "AVAILABILITY_SHOCK"
        enough = mon.answered_calls >= c.abandon_min_sample
        if (enough and mon.abandon_rate >= c.max_abandon_rate) or now < st["fallback_until"]:
            left = max(0.0, st["fallback_until"] - now)
            return (f"abandon rate {mon.abandon_rate:.1%} reached the {c.max_abandon_rate:.0%} limit"
                    + (f", cooling down {left:.0f}s" if left else "")), 0.0, "ABANDON_GUARD"
        if enough and mon.abandon_rate > 0.5 * c.max_abandon_rate:
            # taper: full overflow at half the limit, none at the limit
            scale = max(0.0, (c.max_abandon_rate - mon.abandon_rate) / (0.5 * c.max_abandon_rate))
            return None, scale, "ABANDON_GUARD"
        return None, 1.0, "ABANDON_GUARD"

    def effective_mode(self, conn, campaign_id: str, mode: str, now: float) -> str:
        """Read-only peek used by the worker to pick which pacing engine to run. The binding check
        happens again inside review()."""
        st = self._load(conn, campaign_id, now)
        mon = self.monitor(conn, campaign_id, now)
        reason, _, _ = self._guard(st, mon, mode, now)
        return "PROGRESSIVE" if reason else "PREDICTIVE"

    # ---------------------------------------------------------------- review
    def review(self, conn, campaign_id: str, proposal: PacingProposal, snap: Snapshot,
               provider_health: Dict[str, dict], now: float) -> ApprovedDispatch:
        c = self._cfg
        st = self._load(conn, campaign_id, now)
        mon = self.monitor(conn, campaign_id, now)
        rules: List[RuleResult] = []
        want = max(0, int(proposal.want))
        n = want
        reject = None

        def cut(rule, limit, detail):
            nonlocal n
            limit = max(0, int(limit))
            if limit < n:
                rules.append(RuleResult(rule, "REDUCE", detail, limit))
                n = limit
            else:
                rules.append(RuleResult(rule, "PASS", detail, limit))

        # Is dialing allowed at all?
        if now - proposal.ts > c.max_staleness_s:
            reject = RuleResult("STALE_PROPOSAL", "REJECT", f"proposal is {now - proposal.ts:.1f}s old (limit {c.max_staleness_s:.0f}s)")
        elif snap.campaign_status != "RUNNING":
            reject = RuleResult("CAMPAIGN_STATE", "REJECT", f"campaign is {snap.campaign_status}")
        else:
            states = [h["state"] for h in provider_health.values()]
            if not states or all(s == "OPEN" for s in states):
                reject = RuleResult("PROVIDER_HEALTH", "REJECT", "every provider breaker is open")
            elif all(s in ("OPEN", "HALF_OPEN") for s in states):
                cut("PROVIDER_HEALTH", 3, "only half-open providers: probing with at most 3 calls")
            else:
                rules.append(RuleResult("PROVIDER_HEALTH", "PASS", "at least one provider is healthy"))
        if reject:
            rules.append(reject)
            n = 0

        # Availability shock (persisted history of agents online)
        hist = [(t, o) for t, o in st["online"] if now - t <= c.shock_window_s] + [(now, snap.online)]
        st["online"] = hist[-120:]
        peak = max(o for _, o in hist)
        if peak >= 10 and (peak - snap.online) >= c.shock_drop_fraction * peak and now >= st["shock_until"]:
            st["shock_until"] = now + c.shock_cooldown_s
        reason, scale, gname = self._guard(st, mon, proposal.mode, now)
        if reason and gname == "ABANDON_GUARD" and now >= st["fallback_until"]:
            st["fallback_until"] = now + c.abandon_cooldown_s        # trip: stay progressive for the cooldown

        fallback = False
        if reason:
            # Progressive behaviour: one call per free agent, each tied to its agent. Never more than
            # the free agents, and never more than the engine asked for... except that when the engine
            # asked for a predictive batch, the progressive amount is what we fall back to.
            fallback = proposal.mode != "PROGRESSIVE"
            rules.append(RuleResult(gname, "FALLBACK" if fallback else "PASS", reason, snap.available))
            if fallback and not reject and want > 0:
                n = min(snap.available, snap.eligible_borrowers)      # replace the predictive batch with the progressive one
            bound, ov = min(n, snap.available), 0
            n = bound                      # progressive: never more calls than free agents, whatever was asked
        else:
            if scale < 1.0:
                rules.append(RuleResult("ABANDON_GUARD", "REDUCE", f"abandon rate {mon.abandon_rate:.1%} (limit {c.max_abandon_rate:.0%}): "
                                                                   f"overflow scaled to {scale:.0%}"))
            share_cap = int(c.max_overflow_fraction * snap.online) - snap.unanswered_overflow
            risk_total = self._risk_cap(snap, mon) - snap.unanswered_overflow
            allowed = max(0, min(share_cap, risk_total))
            allowed = int(allowed * scale)
            rules.append(RuleResult("OVERFLOW_RISK", "REDUCE" if n > allowed else "PASS",
                                    f"own model allows {max(0, risk_total)} more in flight (answer rate <= {mon.p_upper:.0%}); "
                                    f"share cap {max(0, share_cap)}", allowed))
            n = min(n, allowed)
            bound, ov = 0, n

        # Hard ceiling, rate limit, burst: apply to the total whatever the mix
        ceiling = int(c.hard_ceiling_ratio * snap.online) - snap.unanswered
        cut("HARD_CEILING", ceiling, f"unanswered calls may not exceed {c.hard_ceiling_ratio:g} x {snap.online} agents online")
        tokens = min(float(c.max_burst), st["tokens"] + (now - st["tok_ts"]) * c.max_cps)
        cut("RATE_LIMIT", int(tokens), f"{c.max_cps:g} calls/s, {tokens:.0f} tokens")
        cut("BURST", c.max_burst, f"at most {c.max_burst} per dispatch")
        if reject:
            n = 0
        if reason:
            bound, ov = min(bound, n), 0
        else:
            bound, ov = 0, n
        n = bound + ov
        st["tokens"], st["tok_ts"] = tokens - n, now
        self._save(conn, campaign_id, st)

        if reject:
            verdict = "REJECT"
        elif fallback:
            verdict = "FALLBACK_PROGRESSIVE"
        elif n == want:
            verdict = "APPROVE"
        elif n == 0 and want > 0:
            verdict = "REJECT"
        else:
            verdict = "REDUCE"
        eff = "PROGRESSIVE" if reason else "PREDICTIVE"
        return ApprovedDispatch(campaign_id, verdict, eff, n, bound, ov, rules, want, _seal=_MINT)

    # ---------------------------------------------------------------- shedding
    def overflow_allowance(self, conn, campaign_id: str, mode: str, snap: Snapshot, now: float) -> int:
        """How many unbound, unanswered calls the campaign may keep in flight right now."""
        c = self._cfg
        st = self._load(conn, campaign_id, now)
        mon = self.monitor(conn, campaign_id, now)
        if mode == "PROGRESSIVE" or now < st["fallback_until"]:
            return 0
        if mon.resolved_300 < c.min_samples_predictive:
            return 0
        # An availability shock only blocks NEW overflow; calls already ringing are trimmed to what the
        # risk model supports with the agents that are left.
        share = int(c.max_overflow_fraction * snap.online)
        return max(0, min(share, self._risk_cap(snap, mon)))

    def note_availability(self, conn, campaign_id, snap: Snapshot, now: float) -> bool:
        """Called every tick, whether or not anyone dispatches, so a shock is noticed within a tick."""
        c = self._cfg
        st = self._load(conn, campaign_id, now)
        hist = [(t, o) for t, o in st["online"] if now - t <= c.shock_window_s] + [(now, snap.online)]
        st["online"] = hist[-120:]
        peak = max(o for _, o in hist)
        tripped = False
        if peak >= 10 and (peak - snap.online) >= c.shock_drop_fraction * peak and now >= st["shock_until"]:
            st["shock_until"] = now + c.shock_cooldown_s
            tripped = True
        self._save(conn, campaign_id, st)
        return tripped
