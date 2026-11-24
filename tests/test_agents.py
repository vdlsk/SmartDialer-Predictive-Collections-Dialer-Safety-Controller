"""What happens when the agent disappears mid-setup, mid-call, or just wants a break."""
from helpers import Env


def active(env, cid):
    return env.call(cid)


def test_agent_lost_while_dialing_is_replaced_by_a_free_agent(tmp_path):
    env = Env(tmp_path, agents=2, borrowers=1)                  # one borrower, so one call, one spare agent
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    first = env.call(cid)["agent_id"]
    assert env.worker.agents.disconnect(first) == "REBOUND"
    c = env.call(cid)
    assert c["agent_id"] not in (None, first) and env.agent(c["agent_id"])["state"] == "DIALING"
    assert env.agent(first)["state"] == "OFFLINE"
    env.event(cid, "ANSWERED")
    assert env.call(cid)["state"] == "CONNECTED"


def test_progressive_cancels_a_call_whose_agent_vanished(tmp_path):
    env = Env(tmp_path, agents=1, borrowers=5)
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    assert env.worker.agents.disconnect("ag0") == "UNBOUND"
    assert env.call(cid)["agent_id"] is None
    env.clock.advance(0.5)
    env.worker.shed(env.cid)                                    # progressive allows zero unbound calls
    assert env.call(cid)["state"] == "CANCELLED"
    assert cid in [r["call_id"] for r in env.db.conn().execute("SELECT call_id FROM provider_commands")]
    b = env.db.conn().execute("SELECT status, attempts FROM borrowers WHERE id=?", (env.call(cid)["borrower_id"],)).fetchone()
    assert b["status"] == "PENDING" and b["attempts"] == 0      # no penalty for the borrower


def test_answered_call_with_no_agent_is_held_then_abandoned_and_counted(tmp_path):
    env = Env(tmp_path, agents=1, borrowers=5)
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    env.worker.agents.disconnect("ag0")
    env.event(cid, "ANSWERED")
    assert env.call(cid)["state"] == "ANSWERED" and env.call(cid)["agent_id"] is None
    env.clock.advance(env.safety_cfg.hold_grace_s + 0.1)
    assert env.worker.calls.expire_holds(env.cid) == 1
    assert env.call(cid)["state"] == "ABANDONED"
    tot = {r["key"]: r["val"] for r in env.db.conn().execute("SELECT key,SUM(val) val FROM buckets GROUP BY key")}
    assert tot["abandoned"] == 1


def test_held_call_is_served_when_an_agent_logs_in(tmp_path):
    env = Env(tmp_path, agents=1, borrowers=5)
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    env.worker.agents.disconnect("ag0")
    env.event(cid, "ANSWERED")
    env.worker.agents.login(env.cid, "ag-new")
    assert env.call(cid)["state"] == "CONNECTED" and env.call(cid)["agent_id"] == "ag-new"


def test_agent_lost_mid_conversation_hands_over_or_hangs_up(tmp_path):
    env = Env(tmp_path, agents=2, borrowers=1)
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    env.event(cid, "ANSWERED")
    a = env.call(cid)["agent_id"]
    assert env.worker.agents.disconnect(a) == "REBOUND"          # spare agent takes over
    assert env.call(cid)["state"] == "CONNECTED"
    b = env.call(cid)["agent_id"]
    assert b != a
    assert env.worker.agents.disconnect(b) == "ENDED"            # nobody left
    assert env.call(cid)["state"] == "COMPLETED" and env.call(cid)["outcome"] == "AGENT_DROPPED"


def test_soft_pause_waits_for_the_call_to_finish(tmp_path):
    env = Env(tmp_path, agents=1, borrowers=5)
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    env.event(cid, "ANSWERED")
    assert env.worker.agents.pause("ag0") == "PAUSE_PENDING"
    assert env.agent("ag0")["state"] == "CONNECTED"
    env.event(cid, "COMPLETED", talk=20)
    assert env.agent("ag0")["state"] == "WRAP_UP"
    env.clock.advance(env.cfg.timing.wrap_up_s + 1)
    env.worker.agents.release_wrapups(env.cid)
    assert env.agent("ag0")["state"] == "PAUSED"
    env.dispatch()                                              # a paused agent is never given a call
    assert env.calls_in("INITIATED") == []


def test_idle_agent_pauses_immediately_and_can_resume(tmp_path):
    env = Env(tmp_path, agents=1, borrowers=5)
    assert env.worker.agents.pause("ag0") == "PAUSED"
    env.dispatch()
    assert env.calls_in("RESERVED", "INITIATED") == []
    env.worker.agents.login(env.cid, "ag0")
    env.dispatch()
    assert len(env.calls_in("INITIATED")) == 1


def test_every_agent_transition_is_legal(tmp_path):
    from smartdialer.states import AGENT_TRANSITIONS, AgentState
    env = Env(tmp_path, agents=3, borrowers=5)
    env.dispatch()
    for cid in env.calls_in("INITIATED"):
        env.event(cid, "ANSWERED")
        env.event(cid, "COMPLETED", talk=5)
    env.worker.agents.disconnect("ag1")
    for r in env.db.conn().execute("SELECT from_state,to_state FROM transitions WHERE entity='agent' AND from_state IS NOT NULL"):
        if r["from_state"] == "OFFLINE" and r["to_state"] == "AVAILABLE":
            continue
        assert AgentState(r["to_state"]) in AGENT_TRANSITIONS[AgentState(r["from_state"])], r
