"""SQLite as the single source of truth.

Why SQLite: the whole point of the prototype is the *protocol* (conditional updates, fencing
tokens, leases, idempotent event ingest). SQLite lets anyone run it with zero setup and still
gives real multi-connection concurrency (WAL + BEGIN IMMEDIATE). Every statement that matters is
written as a compare-and-swap (UPDATE ... WHERE state = expected, check rowcount), so the same SQL
works unchanged on PostgreSQL under READ COMMITTED, where it would also be what stops a double
reservation. What it makes harder: a single writer. See docs/ARCHITECTURE.md for where that bites.
"""
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(
  id TEXT PRIMARY KEY, name TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
  dispatch_owner TEXT, dispatch_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, state TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0, call_id TEXT, state_since REAL NOT NULL,
  reserved_until REAL, wrap_until REAL, pending_state TEXT
);
CREATE INDEX IF NOT EXISTS ix_agents_pick ON agents(campaign_id, state, state_since);
CREATE INDEX IF NOT EXISTS ix_agents_call ON agents(call_id);
CREATE TABLE IF NOT EXISTS borrowers(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, phone TEXT NOT NULL,
  status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0, calls_made INTEGER NOT NULL DEFAULT 0, system_failures INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0, claimed_by_call TEXT, claim_until REAL
);
CREATE INDEX IF NOT EXISTS ix_borrowers_pick ON borrowers(campaign_id, status, priority DESC, next_attempt_at);
CREATE TABLE IF NOT EXISTS calls(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, borrower_id TEXT NOT NULL, agent_id TEXT,
  kind TEXT NOT NULL, provider TEXT, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
  attempt_no INTEGER NOT NULL, created_at REAL NOT NULL, state_since REAL NOT NULL,
  initiated_at REAL, answered_at REAL, connected_at REAL, ended_at REAL,
  hold_deadline REAL, outcome TEXT, reason TEXT, placed_by TEXT, checked_at REAL,
  UNIQUE(borrower_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_calls_state ON calls(campaign_id, state);
CREATE INDEX IF NOT EXISTS ix_calls_agent ON calls(agent_id);
CREATE TABLE IF NOT EXISTS provider_events(
  provider TEXT NOT NULL, event_id TEXT NOT NULL, call_id TEXT NOT NULL, type TEXT NOT NULL,
  provider_ts REAL, received_at REAL NOT NULL, disposition TEXT,
  PRIMARY KEY(provider, event_id)
);
CREATE INDEX IF NOT EXISTS ix_pe_call ON provider_events(call_id);
CREATE TABLE IF NOT EXISTS provider_commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, provider TEXT NOT NULL,
  cmd TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0, UNIQUE(call_id, cmd)
);
CREATE TABLE IF NOT EXISTS transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, cause TEXT, worker TEXT
);
CREATE INDEX IF NOT EXISTS ix_tr_entity ON transitions(entity, entity_id);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, campaign_id TEXT NOT NULL,
  worker TEXT, mode TEXT, requested INTEGER, approved INTEGER, bound INTEGER, overflow INTEGER,
  verdict TEXT, effective_mode TEXT, reasons TEXT, explain TEXT
);
CREATE TABLE IF NOT EXISTS buckets(
  campaign_id TEXT NOT NULL, bucket INTEGER NOT NULL, key TEXT NOT NULL, val REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(campaign_id, bucket, key)
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

from .config import BUCKET_S


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=OFF")      # prototype: speed over durability
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    @contextmanager
    def tx(self):
        """Write transaction. BEGIN IMMEDIATE takes the write lock up front, so a read inside the
        transaction can never go stale before the write that depends on it."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def close(self):
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
            self._local.c = None


def bump(conn, campaign_id: str, now: float, **kv):
    """Add to the 5-second metric buckets that pacing and safety both read.
    Counters live in the database, not in worker memory, so any worker (or a restarted one) sees
    the same history."""
    b = int(now // BUCKET_S)
    for k, v in kv.items():
        conn.execute(
            "INSERT INTO buckets(campaign_id,bucket,key,val) VALUES(?,?,?,?) "
            "ON CONFLICT(campaign_id,bucket,key) DO UPDATE SET val=val+excluded.val",
            (campaign_id, b, k, v))
