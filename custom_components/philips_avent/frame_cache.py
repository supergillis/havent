"""A TTL cache for still frames.

Home Assistant's dashboard polls a camera's still every 10 seconds, and on
the built-in backend the only source of pixels is go2rtc's frame path —
which does not share a running stream: its handler dials the producer, grabs
one keyframe, and stops the producer again. Left uncached, an idle dashboard
therefore opens a complete Tuya session every 10 seconds, enough to exhaust
the camera's 3-5 slot session pool (see stream_server.py).

The TTL throttles *attempts*, not successes: a failing fetch also backs off
for a full TTL and serves the stale frame meanwhile. A thumbnail a minute
old beats a broken image, and it must never turn back into a 10 s hammer.

Nothing here imports Home Assistant.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class FrameCache:
    """Serve a cached frame; attempt a new fetch at most once per TTL."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[bytes | None]],
        *,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._frame: bytes | None = None
        self._attempted_at = float("-inf")
        self._lock = asyncio.Lock()

    async def image(self) -> bytes | None:
        """The freshest frame available, possibly stale, possibly None."""
        async with self._lock:
            if self._clock() - self._attempted_at < self._ttl:
                return self._frame
            self._attempted_at = self._clock()
            try:
                frame = await self._fetch()
            except Exception:  # noqa: BLE001 - any fetch failure serves the stale frame instead
                frame = None
            if frame:
                self._frame = frame
            return self._frame
