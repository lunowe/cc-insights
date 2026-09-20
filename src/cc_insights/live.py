"""The tick between the watcher and the server.

One counter, one condition variable, and no idea what either side does. It
lives in its own module so that `serve.py` -- a read-only server -- does not
have to import the entire write pipeline (`watch` -> `ingest` -> every source
adapter, three times the import cost) just to know when something changed.

The generation number *is* the message. A listener that missed three ticks
does not want three refreshes, it wants the current state, so the payload is
always whatever was true at the last tick and never a queue of what it missed.
"""

from __future__ import annotations

import threading
import time
from typing import Any

#: Seconds a listening dashboard waits before being sent a keep-alive. Under
#: the 60 s that proxies and browsers use to decide a stream is dead.
HEARTBEAT_S = 20.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class LiveState:
    """Publish/subscribe for "the database moved", across two threads."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._generation = 0
        self._payload: dict[str, Any] = {}
        self._started = _now_ms()

    @property
    def generation(self) -> int:
        with self._cv:
            return self._generation

    def publish(self, payload: dict[str, Any]) -> int:
        with self._cv:
            self._generation += 1
            self._payload = payload
            self._cv.notify_all()
            return self._generation

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        with self._cv:
            return self._generation, dict(self._payload)

    def wait_after(self, generation: int, timeout: float) -> tuple[int, dict[str, Any]] | None:
        """Block until the generation passes `generation`, or time out.

        Returns None on timeout, which the caller turns into a keep-alive
        rather than a reconnect.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._generation <= generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            return self._generation, dict(self._payload)

    def status(self) -> dict[str, Any]:
        gen, payload = self.snapshot()
        return {"watching": True, "generation": gen, "since": self._started, "last": payload}


__all__ = ["HEARTBEAT_S", "LiveState"]
