"""Tiny in-process pub/sub used for live UI updates (server-sent events)."""
from __future__ import annotations

import queue
import threading
import time
from collections import deque


class EventBus:
    def __init__(self, keep: int = 300):
        self._subs: list[queue.Queue] = []
        self._ring: deque = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._seq = 0

    def publish(self, kind: str, data: dict | None = None) -> None:
        with self._lock:
            self._seq += 1
            ev = {"id": self._seq, "ts": time.time(), "kind": kind, "data": data or {}}
            self._ring.append(ev)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            return list(self._ring)[-n:]
