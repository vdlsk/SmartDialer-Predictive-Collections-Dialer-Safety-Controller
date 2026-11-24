"""Agent-side operations: login, pause, disconnect, wrap-up expiry."""
from typing import Optional

from . import store
from .calls import CallService
from .clock import Clock
from .db import Database
from .states import AgentState as A


class AgentService:
    def __init__(self, db: Database, clock: Clock, calls: CallService, worker_id: str):
        self.db, self.clock, self.calls, self.worker = db, clock, calls, worker_id

    def login(self, campaign_id: str, agent_id: str) -> str:
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute("SELECT state FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                store.add_agent(conn, campaign_id, agent_id, A.AVAILABLE, now)
                store.log_tr(conn, now, "agent", agent_id, "OFFLINE", "AVAILABLE", "login", self.worker)
            else:
                if row["state"] in ("OFFLINE", "PAUSED"):
                    conn.execute("UPDATE agents SET state='AVAILABLE', version=version+1, state_since=?, pending_state=NULL "
                                 "WHERE id=? AND state IN ('OFFLINE','PAUSED')", (now, agent_id))
                    store.log_tr(conn, now, "agent", agent_id, row["state"], "AVAILABLE", "login", self.worker)
                else:       # already working: just cancel a pending pause/offline request
                    conn.execute("UPDATE agents SET pending_state=NULL WHERE id=?", (agent_id,))
            self.calls.serve_holds(conn, campaign_id, now)
            return "OK"

    def pause(self, agent_id: str) -> str:
        """Soft pause. An idle agent pauses now; a busy one finishes what they are doing first."""
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute("SELECT state FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                return "UNKNOWN"
            if row["state"] == "AVAILABLE":
                cur = conn.execute("UPDATE agents SET state='PAUSED', version=version+1, state_since=? WHERE id=? AND state='AVAILABLE'",
                                   (now, agent_id))
                if cur.rowcount == 1:
                    store.log_tr(conn, now, "agent", agent_id, "AVAILABLE", "PAUSED", "pause", self.worker)
                    return "PAUSED"
            if row["state"] in ("RESERVED", "DIALING", "CONNECTED", "WRAP_UP"):
                conn.execute("UPDATE agents SET pending_state='PAUSED' WHERE id=?", (agent_id,))
                return "PAUSE_PENDING"
            return row["state"]

    def disconnect(self, agent_id: str) -> Optional[str]:
        """Hard loss (browser closed, network gone). Takes effect immediately in any state and
        deals with whatever call the agent was holding."""
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute("SELECT state, call_id, campaign_id FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None or row["state"] == "OFFLINE":
                return None
            conn.execute("UPDATE agents SET state='OFFLINE', call_id=NULL, version=version+1, state_since=?, "
                         "reserved_until=NULL, wrap_until=NULL, pending_state=NULL WHERE id=?", (now, agent_id))
            store.log_tr(conn, now, "agent", agent_id, row["state"], "OFFLINE", "disconnect", self.worker)
            res = self.calls.orphan_of_agent(conn, agent_id, now) if row["call_id"] else None
            return res or "OFFLINE"

    def release_wrapups(self, campaign_id: str) -> int:
        now = self.clock.now()
        with self.db.tx() as conn:
            n = store.release_wrapups(conn, campaign_id, now, self.worker)
            if n:
                self.calls.serve_holds(conn, campaign_id, now)
            return n
