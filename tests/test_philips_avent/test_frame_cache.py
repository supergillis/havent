"""Tests for the still-frame TTL cache used by the builtin backend.

The invariant: fetch attempts happen at most once per TTL, success or
failure. The 10 s dashboard poll must never turn back into a 10 s hammer
on the camera — a failing fetch backs off for a full TTL and serves the
stale frame meanwhile.
"""
import asyncio

from frame_cache import FrameCache


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        result = self.frames.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestFrameCache:
    def test_a_fresh_frame_is_served_without_fetching(self):
        clock = Clock()
        fetch = Fetcher([b"one", b"two"])
        cache = FrameCache(fetch, ttl=60, clock=clock)

        async def go():
            await cache.image()
            clock.now = 59.0
            return await cache.image()

        assert run(go()) == b"one"
        assert fetch.calls == 1

    def test_a_failure_serves_the_stale_frame_and_backs_off_a_full_ttl(self):
        """The load-bearing one: a failed refetch keeps serving the old
        frame, and the next dashboard poll must not retry before the TTL."""
        clock = Clock()
        fetch = Fetcher([b"one", RuntimeError("go2rtc said no"), b"late"])
        cache = FrameCache(fetch, ttl=60, clock=clock)

        async def go():
            await cache.image()
            clock.now = 61.0
            stale = await cache.image()  # the refetch fails
            clock.now = 71.0
            absorbed = await cache.image()  # the next poll, inside the back-off
            clock.now = 122.0
            fresh = await cache.image()
            return stale, absorbed, fresh

        stale, absorbed, fresh = run(go())
        assert stale == b"one"
        assert absorbed == b"one"
        assert fresh == b"late"
        assert fetch.calls == 3  # not 4: the poll at t=71 was absorbed

    def test_concurrent_requests_share_one_fetch(self):
        calls = 0

        async def go():
            nonlocal calls
            gate = asyncio.Event()

            async def fetch():
                nonlocal calls
                calls += 1
                await gate.wait()
                return b"frame"

            cache = FrameCache(fetch, ttl=60, clock=Clock())
            tasks = [asyncio.ensure_future(cache.image()) for _ in range(3)]
            await asyncio.sleep(0)
            gate.set()
            return await asyncio.gather(*tasks)

        assert run(go()) == [b"frame", b"frame", b"frame"]
        assert calls == 1
