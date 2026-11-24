"""Agent and call state machines, written as data so tests and docs can read them."""
from enum import Enum


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"      # picked for a call, provider not yet asked
    DIALING = "DIALING"        # provider accepted the call, borrower not answered
    CONNECTED = "CONNECTED"    # talking
    WRAP_UP = "WRAP_UP"        # after-call work
    PAUSED = "PAUSED"          # agent stepped away


A = AgentState
AGENT_TRANSITIONS = {
    A.OFFLINE:   {A.AVAILABLE, A.PAUSED},
    A.AVAILABLE: {A.RESERVED, A.CONNECTED, A.PAUSED, A.OFFLINE},   # AVAILABLE->CONNECTED: late-bind of an answered overflow call
    A.RESERVED:  {A.DIALING, A.CONNECTED, A.AVAILABLE, A.OFFLINE},
    A.DIALING:   {A.CONNECTED, A.AVAILABLE, A.OFFLINE},
    A.CONNECTED: {A.WRAP_UP, A.OFFLINE},
    A.WRAP_UP:   {A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.PAUSED:    {A.AVAILABLE, A.OFFLINE},
}
# States that hold a call; an agent in one of these cannot be taken by anyone else.
AGENT_BUSY = {A.RESERVED, A.DIALING, A.CONNECTED}


class CallState(str, Enum):
    RESERVED = "RESERVED"      # borrower (and agent, if bound) claimed. QUEUED is the borrower's PENDING status.
    INITIATED = "INITIATED"    # provider accepted place_call
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"      # borrower picked up (provider fact)
    CONNECTED = "CONNECTED"    # bridged to an agent (our fact)
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"          # never reached an answer
    CANCELLED = "CANCELLED"    # we cancelled before answer
    ABANDONED = "ABANDONED"    # borrower answered, no agent in time. The compliance event.


C = CallState
CALL_TERMINAL = {C.COMPLETED, C.FAILED, C.CANCELLED, C.ABANDONED}
CALL_ACTIVE = {C.RESERVED, C.INITIATED, C.RINGING, C.ANSWERED, C.CONNECTED}
CALL_UNANSWERED = {C.RESERVED, C.INITIATED, C.RINGING}

# Forward-only rank. A provider event can only move a call to a higher rank; terminal wins.
CALL_RANK = {C.RESERVED: 1, C.INITIATED: 2, C.RINGING: 3, C.ANSWERED: 4, C.CONNECTED: 5,
             C.COMPLETED: 9, C.FAILED: 9, C.CANCELLED: 9, C.ABANDONED: 9}

CALL_TRANSITIONS = {
    C.RESERVED:  {C.INITIATED, C.RINGING, C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.INITIATED: {C.RINGING, C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.RINGING:   {C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.ANSWERED:  {C.CONNECTED, C.COMPLETED, C.ABANDONED},
    C.CONNECTED: {C.COMPLETED},
    C.COMPLETED: set(), C.FAILED: set(), C.CANCELLED: set(), C.ABANDONED: set(),
}


def can_transition_agent(a: AgentState, b: AgentState) -> bool:
    return b in AGENT_TRANSITIONS[a]


def can_transition_call(a: CallState, b: CallState) -> bool:
    return b in CALL_TRANSITIONS[a]
