"""The only surface the dialer knows about a telecom provider.

Contract (what a real adapter such as Plivo must honour):
* place_call is idempotent on req.call_id. Calling it twice never creates two calls.
* ProviderError  = definitely not placed. ProviderTimeout = unknown, may exist.
* Events are delivered at-least-once, in any order, possibly late. Whoever consumes them (CallService)
  must not assume anything else. In production they arrive by webhook (see server.py);
  poll_events exists so the simulator can pull them on virtual time.
"""
from abc import ABC, abstractmethod
from typing import List

from ..models import DialRequest, ProviderEvent, ProviderCallStatus


class TelecomProvider(ABC):
    name: str

    @abstractmethod
    def place_call(self, req: DialRequest) -> None: ...

    @abstractmethod
    def cancel_call(self, call_id: str) -> None:
        """Cancel if ringing, hang up if answered. Idempotent. Unknown call_id is not an error."""

    @abstractmethod
    def get_call_status(self, call_id: str) -> ProviderCallStatus: ...

    @abstractmethod
    def poll_events(self) -> List[ProviderEvent]: ...
