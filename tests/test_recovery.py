"""Failure case 1: worker crash between reserving and finishing the dial."""
import pytest

from helpers import Env, ScriptedProvider
from smartdialer.allocator import WorkerCrash
from smartdialer.models import ProviderCallStatus


def test_crash_after_reserve_before_provider_call(tmp_path):
    env = Env(tmp_path, agents=3, borrowers=10)
    env.worker.allocator.crash_at = "after_reserve"
    with pytest.raises(WorkerCrash):
        env.dispatch()
    # agent + borrower + call are reserved, provider never heard of it
    assert env.p.placed == []
    assert set(env.states_of_agents().values()) == {"RESERVED"}
    assert len(env.calls_in("RESERVED")) == 3
    # nothing happens inside the lease ...
    env.clock.advance(2)
    env.worker.recon.run(env.cid)
    assert len(env.calls_in("RESERVED")) == 3
    # ... then any worker's reconciler finds out the provider has no such call and undoes it
    env.clock.advance(env.cfg.timing.reserve_lease_s + 1)
    env.worker.recon.run(env.cid)
    assert env.calls_in("RESERVED") == []
    assert set(env.states_of_agents().values()) == {"AVAILABLE"}
    assert env.db.conn().execute("SELECT COUNT(*) FROM borrowers WHERE status='CLAIMED'").fetchone()[0] == 0
    # and the system dials again normally afterwards
    env.clock.advance(10)
    env.dispatch()
    assert len(env.p.placed) == 3


def test_crash_after_provider_accepted_adopts_the_live_call(tmp_path):
    env = Env(tmp_path, agents=2, borrowers=10)
    env.worker.allocator.crash_at = "after_place"
    with pytest.raises(WorkerCrash):
        env.dispatch()
    cid = env.p.placed[0]
    assert env.call(cid)["state"] == "RESERVED"                # we never recorded INITIATED
    env.p.statuses[cid] = ProviderCallStatus(True, "RINGING")
    env.clock.advance(env.cfg.timing.reserve_lease_s + 1)
    env.worker.recon.run(env.cid)
    assert env.call(cid)["state"] == "RINGING" and env.call(cid)["provider"] == "A"
    # the answer arrives and the agent that was reserved for this call takes it
    env.event(cid, "ANSWERED")
    assert env.call(cid)["state"] == "CONNECTED"
    assert env.agent(env.call(cid)["agent_id"])["state"] == "CONNECTED"
    assert env.worker.recon.counters["adopted"] >= 1


def test_unreachable_provider_never_causes_a_double_dial(tmp_path):
    """If the provider cannot be asked, absence of evidence is not evidence: keep the reservation."""
    env = Env(tmp_path, agents=1, borrowers=5)
    env.worker.allocator.crash_at = "after_reserve"
    with pytest.raises(WorkerCrash):
        env.dispatch()
    env.p.unreachable = True
    env.clock.advance(env.cfg.timing.reserve_lease_s + 1)
    env.worker.recon.run(env.cid)
    assert len(env.calls_in("RESERVED")) == 1
    env.p.unreachable = False
    env.clock.advance(env.cfg.timing.recheck_s + 1)
    env.worker.recon.run(env.cid)
    assert env.calls_in("RESERVED") == []


def test_orphan_reservations_are_repaired(tmp_path):
    """Should never happen (everything is one transaction), but if it does the system heals and counts it."""
    env = Env(tmp_path, agents=1, borrowers=2)
    env.dispatch()
    with env.db.tx() as c:                                       # simulate corruption: call row vanishes
        c.execute("DELETE FROM calls")
    env.clock.advance(4000)
    env.worker.recon.run(env.cid)
    assert env.agent("ag0")["state"] == "AVAILABLE"
    assert env.worker.recon.counters["orphans_repaired"] >= 2
