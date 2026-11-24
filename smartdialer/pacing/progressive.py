"""Progressive pacing: one free agent, one call. Never asks for more than the agents it can see."""
from ..models import PacingProposal, Snapshot


class ProgressivePacing:
    mode = "PROGRESSIVE"

    def propose(self, snap: Snapshot, now: float) -> PacingProposal:
        want = max(0, min(snap.available, snap.eligible_borrowers))
        text = (f"Progressive: {snap.available} agent{'' if snap.available == 1 else 's'} free and none are promised to another call, "
                f"so start {want} call{'s' if want != 1 else ''} (one per agent)")
        if snap.eligible_borrowers < snap.available:
            text += f"; only {snap.eligible_borrowers} borrowers are due"
        return PacingProposal("PROGRESSIVE", want, now, {"available": snap.available, "text": text})
