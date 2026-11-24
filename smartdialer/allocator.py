"""Call Allocator: turns an ApprovedDispatch into reserved agents, claimed borrowers and call rows,
then hands the calls to the provider router.

Two phases, on purpose:
  1. allocate()  runs INSIDE the campaign's write transaction, together with the Safety Controller's
                 review. Reservations are committed atomically with the decision that allowed them.
  2. place()     runs AFTER commit. No database lock is ever held across a network call: a slow
                 provider must not freeze every other worker. If the worker dies between the two
                 phases, the call sits in RESERVED with a lease and the reconciler finishes or
                 undoes it (housekeeping.py).
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import store
from .calls import CallService
from .clock import Clock
from .config import CampaignConfig
from .models import ApprovedDispatch, DialRequest, ProviderError, ProviderTimeout
from .providers.router import ProviderRouter
from .safety import is_authentic
from .states import AgentState as A


class WorkerCrash(Exception):
    """Raised by fault injection to simulate a process dying at an exact point."""


@dataclass
class Created:
    call_id: str
    phone: str
    campaign_id: str
    bound: bool


class UnauthorizedDispatch(Exception):
    pass


class CallAllocator:
    def __init__(self, calls: CallService, router: ProviderRouter, clock: Clock,
                 campaigns: Dict[str, CampaignConfig], worker_id: str):
        self.calls, self.router, self.clock, self.campaigns, self.worker = calls, router, clock, campaigns, worker_id
        self.crash_at: Optional[str] = None      # "after_reserve" | "after_place" (fault injection, one-shot)

    def allocate(self, conn, decision: ApprovedDispatch, now: float) -> List[Created]:
        if not is_authentic(decision):
            raise UnauthorizedDispatch("allocator only accepts decisions minted by the Safety Controller")
        if decision.total <= 0:
            return []
        cid = decision.campaign_id
        cfg = self.campaigns[cid]
        agent_ids = store.pick_available_agents(conn, cid, decision.max_bound)
        want = len(agent_ids) + decision.max_overflow
        call_ids = [store.new_id("call") for _ in range(want)]
        until = now + 3600.0
        claimed = store.claim_borrowers(conn, cid, want, call_ids, now, until)
        out: List[Created] = []
        for i, (bid, phone, attempt_no, call_id) in enumerate(claimed):
            agent_id = None
            if i < len(agent_ids):
                if store.reserve_agent(conn, agent_ids[i], call_id, now, A.RESERVED, now + cfg.timing.agent_reservation_s,
                                       self.worker, "allocate"):
                    agent_id = agent_ids[i]
                else:
                    # Lost the race for this agent. Hand the borrower back and do not dial.
                    conn.execute("UPDATE borrowers SET status='PENDING', claimed_by_call=NULL, claim_until=NULL, calls_made=calls_made-1 WHERE id=?", (bid,))
                    continue
            store.insert_call(conn, call_id=call_id, campaign_id=cid, borrower_id=bid, agent_id=agent_id,
                              kind="BOUND" if agent_id else "OVERFLOW", attempt_no=attempt_no, now=now, worker=self.worker)
            out.append(Created(call_id, phone, cid, agent_id is not None))
        return out

    def place(self, created: List[Created]) -> Dict[str, int]:
        res = {"placed": 0, "failed": 0}
        if self.crash_at == "after_reserve":
            self.crash_at = None
            raise WorkerCrash("crashed after reserving agent+borrower, before calling the provider")
        for c in created:
            cfg = self.campaigns[c.campaign_id]
            try:
                name = self.router.place(DialRequest(c.call_id, c.phone, c.campaign_id), cfg.timing.place_retries)
            except ProviderTimeout as e:
                self.calls.fail_placement(c.call_id, "provider_timeout", getattr(e, "provider", None), ambiguous=True)
                res["failed"] += 1
                continue
            except ProviderError as e:
                self.calls.fail_placement(c.call_id, str(e), None, ambiguous=False)
                res["failed"] += 1
                continue
            if self.crash_at == "after_place":
                self.crash_at = None
                raise WorkerCrash("crashed after the provider accepted the call, before recording INITIATED")
            self.calls.mark_initiated(c.call_id, name)
            res["placed"] += 1
        return res
