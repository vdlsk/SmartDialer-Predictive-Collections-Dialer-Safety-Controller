"""SQL primitives. Every state change is a compare-and-swap: the WHERE clause names the state (and
for agents the owning call) we believe is current, and the caller checks rowcount. A worker holding
stale information therefore changes nothing instead of corrupting something."""
import uuid
from typing import List, Optional, Tuple

from .models import Snapshot
from .states import AgentState as A, CallState as C


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def log_tr(conn, now, entity, eid, frm, to, cause, worker):
    conn.execute("INSERT INTO transitions(ts,entity,entity_id,from_state,to_state,cause,worker) VALUES(?,?,?,?,?,?,?)",
                 (now, entity, eid, frm, to, cause, worker))


# ----------------------------------------------------------------------------- agents
def add_agent(conn, campaign_id, agent_id, state, now):
    conn.execute("INSERT OR IGNORE INTO agents(id,campaign_id,state,state_since) VALUES(?,?,?,?)",
                 (agent_id, campaign_id, state.value if hasattr(state, "value") else state, now))


def pick_available_agents(conn, campaign_id, n) -> List[str]:
    """Longest-idle first: the fairest cheap rule, and the index serves it directly."""
    if n <= 0:
        return []
    rows = conn.execute("SELECT id FROM agents WHERE campaign_id=? AND state='AVAILABLE' "
                        "ORDER BY state_since LIMIT ?", (campaign_id, n)).fetchall()
    return [r["id"] for r in rows]


def reserve_agent(conn, agent_id, call_id, now, to_state: A = A.RESERVED, until: Optional[float] = None,
                  worker="", cause="allocate") -> bool:
    """THE guard against double allocation. Two workers can both have read this agent as AVAILABLE;
    only one UPDATE finds state='AVAILABLE' still true. The other sees rowcount 0 and moves on."""
    cur = conn.execute(
        "UPDATE agents SET state=?, call_id=?, version=version+1, state_since=?, reserved_until=? "
        "WHERE id=? AND state='AVAILABLE'", (to_state.value, call_id, now, until, agent_id))
    if cur.rowcount == 1:
        log_tr(conn, now, "agent", agent_id, "AVAILABLE", to_state.value, cause, worker)
        return True
    return False


def claim_one_agent(conn, campaign_id, call_id, now, to_state, worker="", cause="late_bind") -> Optional[str]:
    for aid in pick_available_agents(conn, campaign_id, 3):
        if reserve_agent(conn, aid, call_id, now, to_state, None, worker, cause):
            return aid
    return None


def release_agent(conn, now, agent_id, call_id, *, wrap_s: Optional[float] = None, worker="", cause="") -> Optional[str]:
    """Fenced by call_id: if the agent already belongs to a different call (or none), do nothing.
    Returns the new state, or None if the fence rejected us."""
    row = conn.execute("SELECT state, pending_state FROM agents WHERE id=? AND call_id=?",
                       (agent_id, call_id)).fetchone()
    if row is None:
        return None
    if wrap_s is not None:
        new, wrap_until = A.WRAP_UP.value, now + wrap_s
    else:
        new, wrap_until = (row["pending_state"] or A.AVAILABLE.value), None
    pending = row["pending_state"] if new == A.WRAP_UP.value else None
    cur = conn.execute(
        "UPDATE agents SET state=?, call_id=NULL, version=version+1, state_since=?, reserved_until=NULL, "
        "wrap_until=?, pending_state=? WHERE id=? AND call_id=? AND state=?",
        (new, now, wrap_until, pending, agent_id, call_id, row["state"]))
    if cur.rowcount != 1:
        return None
    log_tr(conn, now, "agent", agent_id, row["state"], new, cause, worker)
    return new


def release_wrapups(conn, campaign_id, now, worker="") -> int:
    rows = conn.execute("SELECT id, pending_state FROM agents WHERE campaign_id=? AND state='WRAP_UP' AND wrap_until<=?",
                        (campaign_id, now)).fetchall()
    n = 0
    for r in rows:
        new = r["pending_state"] or A.AVAILABLE.value
        cur = conn.execute("UPDATE agents SET state=?, version=version+1, state_since=?, wrap_until=NULL, pending_state=NULL "
                           "WHERE id=? AND state='WRAP_UP' AND wrap_until<=?", (new, now, r["id"], now))
        if cur.rowcount == 1:
            log_tr(conn, now, "agent", r["id"], "WRAP_UP", new, "wrap_up_done", worker)
            n += 1
    return n


# ----------------------------------------------------------------------------- borrowers
def add_borrowers(conn, campaign_id, rows: List[Tuple[str, str, int]]):
    conn.executemany("INSERT OR IGNORE INTO borrowers(id,campaign_id,phone,status,priority) VALUES(?,?,?,'PENDING',?)",
                     [(i, campaign_id, p, pr) for i, p, pr in rows])


def claim_borrowers(conn, campaign_id, n, call_ids: List[str], now, until) -> List[Tuple[str, str, int, str]]:
    """Claim up to n borrowers, highest priority first. Returns (borrower_id, phone, attempt_no, call_id).
    attempt_no counts every dial ever made to this borrower (UNIQUE with borrower_id on calls), which is
    different from `attempts`, the number of tries that count against the retry limit."""
    if n <= 0:
        return []
    rows = conn.execute("SELECT id, phone, calls_made FROM borrowers WHERE campaign_id=? AND status='PENDING' "
                        "AND next_attempt_at<=? ORDER BY priority DESC, next_attempt_at LIMIT ?",
                        (campaign_id, now, n)).fetchall()
    out = []
    for r, cid in zip(rows, call_ids):
        cur = conn.execute("UPDATE borrowers SET status='CLAIMED', claimed_by_call=?, claim_until=?, calls_made=calls_made+1 "
                           "WHERE id=? AND status='PENDING'", (cid, until, r["id"]))
        if cur.rowcount == 1:
            out.append((r["id"], r["phone"], r["calls_made"] + 1, cid))
    return out


def borrower_after(conn, borrower_id, call_id, now, *, outcome: str, max_attempts: int, backoff_s: float,
                   system_retry_s: float, max_system_failures: int, abandoned_callback_s: float):
    """Decide what happens to the borrower after a call ends. Fenced by claimed_by_call."""
    b = conn.execute("SELECT attempts, system_failures FROM borrowers WHERE id=? AND claimed_by_call=?",
                     (borrower_id, call_id)).fetchone()
    if b is None:
        return
    attempts, sysf = b["attempts"], b["system_failures"]
    status, nxt = "PENDING", now
    if outcome == "DONE":
        status = "DONE"
    elif outcome in ("NO_ANSWER", "BUSY"):
        attempts += 1
        if attempts >= max_attempts:
            status = "EXHAUSTED"
        nxt = now + backoff_s * (2 ** (attempts - 1))
    elif outcome == "SYSTEM":
        sysf += 1
        nxt = now + min(60.0, system_retry_s * (2 ** (sysf - 1)))
        if sysf > max_system_failures:
            attempts, sysf = attempts + 1, 0
            if attempts >= max_attempts:
                status = "EXHAUSTED"
    elif outcome == "ABANDONED":
        nxt = now + abandoned_callback_s
    elif outcome == "CANCELLED":
        pass
    conn.execute("UPDATE borrowers SET status=?, attempts=?, system_failures=?, next_attempt_at=?, "
                 "claimed_by_call=NULL, claim_until=NULL WHERE id=? AND claimed_by_call=?",
                 (status, attempts, sysf, nxt, borrower_id, call_id))


# ----------------------------------------------------------------------------- calls
def get_call(conn, call_id):
    return conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()


def insert_call(conn, *, call_id, campaign_id, borrower_id, agent_id, kind, attempt_no, now, worker):
    conn.execute("INSERT INTO calls(id,campaign_id,borrower_id,agent_id,kind,state,attempt_no,created_at,state_since,placed_by) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (call_id, campaign_id, borrower_id, agent_id, kind, C.RESERVED.value, attempt_no, now, now, worker))
    log_tr(conn, now, "call", call_id, "QUEUED", C.RESERVED.value, "allocate", worker)


def set_call(conn, call_id, expect: Tuple[str, ...], new: str, now, cause, worker, **fields) -> bool:
    sets = "".join(f", {k}=?" for k in fields)
    q = (f"UPDATE calls SET state=?, version=version+1, state_since=?{sets} "
         f"WHERE id=? AND state IN ({','.join('?' * len(expect))})")
    prev = conn.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()
    cur = conn.execute(q, (new, now, *fields.values(), call_id, *expect))
    if cur.rowcount == 1:
        log_tr(conn, now, "call", call_id, prev["state"] if prev else None, new, cause, worker)
        return True
    return False


# ----------------------------------------------------------------------------- snapshot
def take_snapshot(conn, campaign_id, now, horizon_s: float) -> Snapshot:
    cnt = {s.value: 0 for s in A}
    for r in conn.execute("SELECT state, COUNT(*) n FROM agents WHERE campaign_id=? GROUP BY state", (campaign_id,)):
        cnt[r["state"]] = r["n"]
    ending = conn.execute("SELECT COUNT(*) FROM agents WHERE campaign_id=? AND state='WRAP_UP' AND wrap_until<=?",
                          (campaign_id, now + horizon_s)).fetchone()[0]
    ub = uo = 0
    for r in conn.execute("SELECT agent_id IS NOT NULL AS bound, COUNT(*) n FROM calls WHERE campaign_id=? "
                          "AND state IN ('RESERVED','INITIATED','RINGING') GROUP BY 1", (campaign_id,)):
        if r["bound"]:
            ub = r["n"]
        else:
            uo = r["n"]
    held = conn.execute("SELECT COUNT(*) FROM calls WHERE campaign_id=? AND state='ANSWERED'", (campaign_id,)).fetchone()[0]
    elig = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM borrowers WHERE campaign_id=? AND status='PENDING' "
                        "AND next_attempt_at<=? LIMIT 5000)", (campaign_id, now)).fetchone()[0]
    status = conn.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    online = sum(cnt[s] for s in ("AVAILABLE", "RESERVED", "DIALING", "CONNECTED", "WRAP_UP"))
    return Snapshot(ts=now, online=online, available=cnt["AVAILABLE"], reserved=cnt["RESERVED"],
                    dialing=cnt["DIALING"], connected=cnt["CONNECTED"], wrap_up=cnt["WRAP_UP"],
                    wrap_ending_soon=ending, paused=cnt["PAUSED"], offline=cnt["OFFLINE"],
                    unanswered_bound=ub, unanswered_overflow=uo, held=held, eligible_borrowers=elig,
                    campaign_status=status["status"] if status else "UNKNOWN")
