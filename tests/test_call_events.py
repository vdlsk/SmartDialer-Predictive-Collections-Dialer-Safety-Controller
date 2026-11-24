"""Providers send duplicates, reorder, and workers die. The call must still end up sensible."""
import pytest

from helpers import Env, ScriptedProvider


def dial_one(tmp_path, **kw):
    env = Env(tmp_path, agents=1, borrowers=5, **kw)
    env.dispatch()
    cid = env.calls_in("INITIATED")[0]
    return env, cid


def test_answered_answered_answered_completed(tmp_path):
    env, cid = dial_one(tmp_path)
    assert env.event(cid, "RINGING") == "APPLIED"
    assert env.event(cid, "ANSWERED") == "APPLIED"
    assert env.event(cid, "ANSWERED") == "STALE"                # different event id, same fact
    assert env.event(cid, "ANSWERED", eid="dup") == "STALE"
    assert env.event(cid, "ANSWERED", eid="dup") == "DUPLICATE"  # same event id again
    assert env.event(cid, "COMPLETED", talk=30) == "APPLIED"
    assert env.event(cid, "ANSWERED") == "IGNORED_TERMINAL"
    assert env.call(cid)["state"] == "COMPLETED"
    assert env.agent("ag0")["state"] == "WRAP_UP"
    seq = env.transitions("call", cid)
    assert seq == [("QUEUED", "RESERVED"), ("RESERVED", "INITIATED"), ("INITIATED", "RINGING"),
                   ("RINGING", "ANSWERED"), ("ANSWERED", "CONNECTED"), ("CONNECTED", "COMPLETED")]
    assert [t for t in env.transitions("agent", "ag0")].count(("DIALING", "CONNECTED")) == 1
    assert env.db.conn().execute("SELECT status FROM borrowers WHERE claimed_by_call IS NULL AND id=?",
                                 (env.call(cid)["borrower_id"],)).fetchone()["status"] == "DONE"


def test_completed_answered_ringing_out_of_order(tmp_path):
    env, cid = dial_one(tmp_path)
    assert env.event(cid, "COMPLETED", talk=45) == "APPLIED_EARLY"
    assert env.event(cid, "ANSWERED") == "IGNORED_TERMINAL"
    assert env.event(cid, "RINGING") == "IGNORED_TERMINAL"
    call = env.call(cid)
    assert call["state"] == "COMPLETED" and call["outcome"] == "ANSWER_NOT_OBSERVED"
    assert env.agent("ag0")["state"] == "AVAILABLE"             # freed once, never connected, no wrap-up
    assert env.agent("ag0")["call_id"] is None
    assert len(env.transitions("call", cid)) == 3               # QUEUED>RESERVED>INITIATED>COMPLETED only
    tot = {r["key"]: r["val"] for r in env.db.conn().execute("SELECT key,SUM(val) val FROM buckets GROUP BY key")}
    assert tot.get("abandoned") == 1                           # counted against the abandon budget


def test_duplicate_event_id_changes_nothing(tmp_path):
    env, cid = dial_one(tmp_path)
    env.event(cid, "RINGING", eid="x1")
    before = env.db.conn().execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
    for _ in range(5):
        assert env.event(cid, "RINGING", eid="x1") == "DUPLICATE"
    assert env.db.conn().execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == before


def test_event_for_unknown_call_is_recorded_and_ignored(tmp_path):
    env, _ = dial_one(tmp_path)
    assert env.event("nope", "ANSWERED") == "UNKNOWN_CALL"


def test_no_answer_frees_agent_and_schedules_retry(tmp_path):
    env, cid = dial_one(tmp_path)
    env.event(cid, "RINGING")
    env.event(cid, "NO_ANSWER")
    b = env.db.conn().execute("SELECT * FROM borrowers WHERE id=?", (env.call(cid)["borrower_id"],)).fetchone()
    assert env.call(cid)["state"] == "FAILED"
    assert env.agent("ag0")["state"] == "AVAILABLE"
    assert b["status"] == "PENDING" and b["attempts"] == 1 and b["next_attempt_at"] > env.clock.now()


def test_borrower_exhausted_after_max_attempts(tmp_path):
    env = Env(tmp_path, agents=1, borrowers=1)
    for i in range(env.cfg.retry.max_attempts):
        env.clock.advance(1000)
        env.dispatch()
        cid = env.calls_in("INITIATED")[0]
        env.event(cid, "NO_ANSWER", eid=f"n{i}")
    assert env.db.conn().execute("SELECT status FROM borrowers").fetchone()["status"] == "EXHAUSTED"


def test_ghost_answer_after_we_gave_up_is_hung_up(tmp_path):
    """Provider timed out on us, we failed the call, then the call turned out to be real."""
    env, cid = dial_one(tmp_path)
    env.worker.calls.cancel_call(cid, "test")
    assert env.event(cid, "ANSWERED") == "GHOST_HANGUP"
    cmds = [tuple(r) for r in env.db.conn().execute("SELECT cmd,status FROM provider_commands WHERE call_id=?", (cid,))]
    assert ("CANCEL", "PENDING") in cmds or ("HANGUP", "PENDING") in cmds
    env.worker.recon.send_commands()
    assert cid in env.p.cancelled


def test_crash_after_answered_leaves_nothing_half_done(tmp_path):
    """The worker dies in the middle of processing ANSWERED. The transaction rolls back, including
    the dedupe row, so the redelivered event is processed cleanly."""
    env, cid = dial_one(tmp_path)
    env.event(cid, "RINGING")
    calls = env.worker.calls
    original = calls._bridge

    def boom(*a, **k):
        raise RuntimeError("worker killed")

    calls._bridge = boom
    with pytest.raises(RuntimeError):
        env.event(cid, "ANSWERED", eid="answered-1")
    calls._bridge = original
    assert env.call(cid)["state"] == "RINGING"                  # nothing applied
    assert env.agent("ag0")["state"] == "DIALING"
    assert env.db.conn().execute("SELECT COUNT(*) FROM provider_events WHERE event_id='answered-1'").fetchone()[0] == 0
    assert env.event(cid, "ANSWERED", eid="answered-1") == "APPLIED"   # provider redelivers
    assert env.call(cid)["state"] == "CONNECTED" and env.agent("ag0")["state"] == "CONNECTED"
    assert env.event(cid, "COMPLETED", talk=10) == "APPLIED"
    assert env.agent("ag0")["state"] == "WRAP_UP"


def test_reconciler_recovers_lost_completed_event(tmp_path):
    """COMPLETED is never delivered. The agent must not stay CONNECTED forever."""
    env, cid = dial_one(tmp_path)
    env.event(cid, "RINGING")
    env.event(cid, "ANSWERED")
    from smartdialer.models import ProviderCallStatus
    env.p.statuses[cid] = ProviderCallStatus(True, "ENDED", 40.0)
    env.clock.advance(env.cfg.timing.connected_check_s + 5)
    env.worker.recon.run(env.cid)
    assert env.call(cid)["state"] == "COMPLETED"
    assert env.agent("ag0")["state"] in ("WRAP_UP", "AVAILABLE")


def test_failed_event_after_answer_ends_call_cleanly(tmp_path):
    env, cid = dial_one(tmp_path)
    env.event(cid, "ANSWERED")
    assert env.event(cid, "FAILED") == "APPLIED"
    assert env.call(cid)["state"] == "COMPLETED" and env.call(cid)["outcome"] == "DROPPED"
    assert env.agent("ag0")["state"] == "WRAP_UP"
