"""Two workers see the same free agent at the same moment. Only one may get it."""
import threading

from helpers import Env
from smartdialer import store
from smartdialer.db import Database
from smartdialer.states import AgentState as A


def test_compare_and_swap_lets_exactly_one_reserve(tmp_path):
    """Both workers READ the agent as AVAILABLE before either WRITES (forced with a barrier, and
    with no surrounding transaction, i.e. the weakest isolation we could be running under).
    The conditional UPDATE is what decides; the loser sees rowcount 0."""
    env = Env(tmp_path, agents=1)
    path = str(tmp_path / "t.db")
    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        db = Database(path)
        conn = db.conn()
        seen = store.pick_available_agents(conn, env.cid, 1)       # both see ag0 as free
        barrier.wait()
        results[name] = store.reserve_agent(conn, seen[0], f"call_{name}", 1000.0, A.RESERVED, worker=name)

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("w1", "w2")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results.values()) == [False, True]
    row = env.agent("ag0")
    assert row["state"] == "RESERVED" and row["call_id"] in ("call_w1", "call_w2")


def test_many_workers_never_double_book(tmp_path):
    """Eight threads, each with its own connection and its own worker, all dispatching at once with
    the dispatch lease OFF so nothing but the database protocol stands between them."""
    from smartdialer.worker import DialerWorker
    from smartdialer.providers.router import ProviderRouter
    from helpers import ScriptedProvider
    env = Env(tmp_path, agents=40, borrowers=500)
    errors = []

    def run(i):
        try:
            prov = ScriptedProvider("A")
            w = DialerWorker(f"t{i}", env.db, env.clock, ProviderRouter([prov], env.clock, env.safety_cfg),
                             {env.cid: env.cfg}, env.safety_cfg, use_lease=False)
            for _ in range(25):
                w.dispatch(env.cid)
        except Exception as e:                                       # pragma: no cover
            errors.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    c = env.db.conn()
    busy = c.execute("SELECT COUNT(*) FROM agents WHERE state IN ('RESERVED','DIALING')").fetchone()[0]
    active = c.execute("SELECT COUNT(*) FROM calls WHERE state IN ('RESERVED','INITIATED','RINGING')").fetchone()[0]
    assert busy == 40, "every agent should have been used exactly once"
    assert active == 40, "progressive: one call per agent, never more"
    assert c.execute("SELECT COUNT(DISTINCT agent_id) FROM calls WHERE agent_id IS NOT NULL").fetchone()[0] == 40
    assert c.execute("SELECT COUNT(DISTINCT borrower_id) FROM calls").fetchone()[0] == 40


def test_same_borrower_cannot_be_claimed_twice(tmp_path):
    env = Env(tmp_path, agents=0, borrowers=3)
    with env.db.tx() as c:
        a = store.claim_borrowers(c, env.cid, 3, ["c1", "c2", "c3"], 1000.0, 2000.0)
    with env.db.tx() as c:
        b = store.claim_borrowers(c, env.cid, 3, ["c4", "c5", "c6"], 1000.0, 2000.0)
    assert len(a) == 3 and b == []


def test_unique_attempt_guard_catches_duplicate_job(tmp_path):
    import sqlite3, pytest
    env = Env(tmp_path, agents=1, borrowers=1)
    env.dispatch()
    row = env.db.conn().execute("SELECT * FROM calls").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        with env.db.tx() as c:
            store.insert_call(c, call_id="dup", campaign_id=env.cid, borrower_id=row["borrower_id"], agent_id=None,
                              kind="OVERFLOW", attempt_no=row["attempt_no"], now=1.0, worker="x")


def test_release_is_fenced_by_call_id(tmp_path):
    """A slow worker finishing an old call must not free an agent that has moved on to a new one."""
    env = Env(tmp_path, agents=1)
    env.dispatch()
    call = env.calls_in("INITIATED")[0]
    with env.db.tx() as c:
        assert store.release_agent(c, 1.0, "ag0", "some_old_call") is None
    assert env.agent("ag0")["call_id"] == call and env.agent("ag0")["state"] == "DIALING"
