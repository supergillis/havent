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
    def test_the_first_request_fetches(self):
        fetch = Fetcher([b"one"])
        cache = FrameCache(fetch, ttl=60, clock=Clock())
        assert run(cache.image()) == b"one"
        assert fetch.calls == 1

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

    def test_a_stale_frame_is_refetched(self):
        clock = Clock()
        fetch = Fetcher([b"one", b"two"])
        cache = FrameCache(fetch, ttl=60, clock=clock)

        async def go():
            await cache.image()
            clock.now = 61.0
            return await cache.image()

        assert run(go()) == b"two"
        assert fetch.calls == 2

    def test_a_failed_refetch_serves_the_stale_frame(self):
        clock = Clock()
        fetch = Fetcher([b"one", RuntimeError("go2rtc said no")])
        cache = FrameCache(fetch, ttl=60, clock=clock)

        async def go():
            await cache.image()
            clock.now = 61.0
            return await cache.image()

        assert run(go()) == b"one"

    def test_a_failure_backs_off_for_a_full_ttl(self):
        """The load-bearing one: failure must not retry on the next poll."""
        clock = Clock()
        fetch = Fetcher([RuntimeError("boom"), b"late"])
        cache = FrameCache(fetch, ttl=60, clock=clock)

        async def go():
            first = await cache.image()
            clock.now = 10.0  # the next dashboard poll
            second = await cache.image()
            clock.now = 61.0
            third = await cache.image()
            return first, second, third

        first, second, third = run(go())
        assert first is None
        assert second is None
        assert third == b"late"
        assert fetch.calls == 2  # not 3: the poll at t=10 was absorbed

    def test_an_empty_fetch_is_treated_like_a_failure(self):
        clock = Clock()
        fetch = Fetcher([b"one", None, b"three"])
        cache = FrameCache(fetch, ttl=60, clock=clock)

        async def go():
            await cache.image()
            clock.now = 61.0
            stale = await cache.image()
            clock.now = 122.0
            fresh = await cache.image()
            return stale, fresh

        stale, fresh = run(go())
        assert stale == b"one"
        assert fresh == b"three"

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
