"""Streaming time-window primitives for the Phase 3 feature engine.

Every structure is push-driven and past-only: values enter with their event
timestamp, evictions depend only on the current timestamp, and no structure
ever looks forward. The same objects serve historical replay and live
processing (Critical Rule 3) — there is no vectorized variant.
"""
from __future__ import annotations

from collections import deque


class WindowSum:
    """Rolling sum of values over the trailing `horizon_ns`."""

    __slots__ = ("horizon", "buf", "total")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon = horizon_ns
        self.buf: deque[tuple[int, float]] = deque()
        self.total = 0.0

    def push(self, ts: int, v: float) -> None:
        if v != 0.0:
            self.buf.append((ts, v))
            self.total += v
        self.evict(ts)

    def evict(self, now: int) -> None:
        cut = now - self.horizon
        buf = self.buf
        while buf and buf[0][0] <= cut:
            self.total -= buf.popleft()[1]

    @property
    def sum(self) -> float:
        return self.total


class WindowCount:
    """Rolling event count over the trailing `horizon_ns`."""

    __slots__ = ("horizon", "buf")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon = horizon_ns
        self.buf: deque[int] = deque()

    def push(self, ts: int, n: int = 1) -> None:
        for _ in range(n):
            self.buf.append(ts)
        self.evict(ts)

    def evict(self, now: int) -> None:
        cut = now - self.horizon
        buf = self.buf
        while buf and buf[0] <= cut:
            buf.popleft()

    @property
    def count(self) -> int:
        return len(self.buf)


class WindowPast:
    """Value of a series as of (now − horizon): the most recent sample at or
    before the window boundary. Keeps exactly one sample older than the
    boundary as the prevailing value."""

    __slots__ = ("horizon", "buf", "last")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon = horizon_ns
        self.buf: deque[tuple[int, float]] = deque()
        self.last: float | None = None

    def push(self, ts: int, v: float) -> None:
        self.buf.append((ts, v))
        self.last = v
        self.evict(ts)

    def evict(self, now: int) -> None:
        cut = now - self.horizon
        buf = self.buf
        # drop entries while the NEXT one is still at/before the boundary —
        # the newest at-or-before-boundary sample must survive
        while len(buf) >= 2 and buf[1][0] <= cut:
            buf.popleft()

    def past(self, now: int) -> float | None:
        """Prevailing value at (now − horizon); None during warm-up."""
        cut = now - self.horizon
        buf = self.buf
        if not buf or buf[0][0] > cut:
            return None
        return buf[0][1]


class WindowMin:
    """Rolling minimum over the trailing `horizon_ns` (monotonic deque)."""

    __slots__ = ("horizon", "buf")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon = horizon_ns
        self.buf: deque[tuple[int, float]] = deque()

    def push(self, ts: int, v: float) -> None:
        buf = self.buf
        while buf and buf[-1][1] >= v:
            buf.pop()
        buf.append((ts, v))
        self.evict(ts)

    def evict(self, now: int) -> None:
        cut = now - self.horizon
        buf = self.buf
        while buf and buf[0][0] <= cut:
            buf.popleft()

    def min(self) -> float | None:
        return self.buf[0][1] if self.buf else None


class WindowMean:
    """Rolling arithmetic mean of pushed samples over the trailing window.

    Sample-weighted (one weight per emitted group row) — an intentional v1
    simplification of time-weighting; documented in the Phase 3 report.
    """

    __slots__ = ("horizon", "buf", "total")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon = horizon_ns
        self.buf: deque[tuple[int, float]] = deque()
        self.total = 0.0

    def push(self, ts: int, v: float) -> None:
        self.buf.append((ts, v))
        self.total += v
        self.evict(ts)

    def evict(self, now: int) -> None:
        cut = now - self.horizon
        buf = self.buf
        while buf and buf[0][0] <= cut:
            self.total -= buf.popleft()[1]

    def mean(self) -> float | None:
        return self.total / len(self.buf) if self.buf else None
