# Builtin Backend Session Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the built-in backend from exhausting the camera's session pool. The first field trial saturated an SCD953 so badly that the user's own Philips Avent app could not connect: Home Assistant's 10-second thumbnail poll opened a complete Tuya session per poll (~360/hour) against a pool of 3–5 slots, and abandoned sessions lingered as zombies. This plan removes the churn (snapshot cache), stops us piling onto a full pool (circuit breaker, short answer timeout), frees our own slots promptly (restore `send_disconnect`, own session only), guards the one-consumer invariant, and corrects a wrong conclusion that is currently written into the code, the tests and the design doc.

**Architecture:** All camera-facing changes stay in the three HA-free modules (`sdp.py` untouched, `signaling.py` gains `send_disconnect`, `stream_server.py` gains the breaker/timeout/guard and the corrected lifetime rules). The snapshot fix lives in a new HA-free `frame_cache.py` plus thin glue in `camera.py`. The add-on backend's behaviour is untouched everywhere.

**Tech Stack:** Python 3.11+, bare pytest (no pytest-asyncio — coroutines are driven with `asyncio.run` via a `run()` helper), fakes rather than mocks, ruff with the rule set pinned in `pyproject.toml`.

**Spec reference:** `docs/superpowers/specs/2026-08-09-containerless-camera-design.md` — note its "What shipped" and R8 sections currently assert the *wrong* peer-scoped-disconnect conclusion; Task 8 corrects them. Until then, where this plan and the spec disagree, this plan wins.

**Non-negotiable constraints:**

- `sdp.py`, `signaling.py`, `stream_server.py` and `frame_cache.py` import no Home Assistant, and must stay that way.
- **No live testing against the camera.** The device is in a degraded state and every extra session makes a real baby monitor worse. Every task is verified by the test suite alone; field verification is a separate later step performed by the user (see "Field verification" at the end — it says what to look for in the log, not how to stream).
- Each task leaves `PYTHONPATH=. pytest tests/test_philips_avent/ -v` green and `ruff check custom_components/ examples/ tools/ --ignore E501` clean.
- The add-on backend keeps its current ffmpeg snapshot path and RTSP behaviour, byte-for-byte where possible.

## The verified mechanism (what the code and docs must now say)

Established against go2rtc source, the field-trial logs and Tuya's own TuyaOS IPC firmware docs:

1. **The churn is HA's thumbnail poll.** With `use_stream_for_stills = True` every still goes to go2rtc's `/api/frame.jpeg`, whose handler (`internal/mjpeg/mjpeg.go`) dials the producer, waits for one keyframe, then **stops the producer**. Redial timestamps are phase-locked to a 10.00 s period: an idle dashboard opens a complete Tuya session every 10 seconds. The docstring in `camera.py` claiming snapshots "share the running stream" is false for this backend.
2. **The camera's session pool is 3–5** (`max_client_num` in TuyaOS IPC firmware). When full, the camera forcibly closes the *newcomer* — the vendor app's "Device Busy". Sessions abandoned mid-handshake become zombies reclaimed only on long timers (documented at ~20 minutes).
3. **The `disconnect` frame is session-scoped, not peer-scoped.** A previous revision concluded the opposite and removed the frame entirely; the evidence was misread (the "surviving" session was simply replaced by the next 10 s poll) and contaminated (a second go2rtc was dialling the same endpoint). Both go2rtc's own `pkg/tuya` and this repo's Go bridge (`avent-webrtc-bridge/pkg/rtsp/bridge.go`, `Stop()` → `SendDisconnect()`) send `disconnect` for **their own session id only, at their own teardown**. Ids are per-session; cameras send per-session disconnects. The wire shape (from `mqttCamera.go`): type `disconnect`, protocol 302, body `{"mode": "webrtc"}`, `sessionid` = the sender's own.
4. **Cameras answer in 74–135 ms when they answer at all.** A 20 s answer timeout only holds a doomed dial open.

---

## File Structure

**New files:**
- `custom_components/philips_avent/frame_cache.py` — TTL cache for still frames, no HA imports
- `tests/test_philips_avent/test_frame_cache.py` — unit tests for the cache

**Modified files:**
- `custom_components/philips_avent/camera.py` — builtin stills served from the cache; false docstrings corrected (Task 2)
- `custom_components/philips_avent/signaling.py` — `Session.send_disconnect()`, corrected comments (Task 4)
- `custom_components/philips_avent/stream_server.py` — circuit breaker, disconnect-on-abandonment, 6 s answer timeout, second-consumer guard, corrected module docstring (Tasks 3–6)
- `tests/test_philips_avent/test_stream_server.py` — same tasks; corrected module docstring and test names (Tasks 3–6)
- `docs/superpowers/specs/2026-08-09-containerless-camera-design.md` — "What shipped", R5, R8 corrected (Task 8)
- `WHITEPAPER.md` — new subsection on the session pool (Task 9)

## Parallelization — who owns which files

Tasks will be implemented by separate agents. Tasks in different tracks touch disjoint files and may run in parallel; tasks inside a track share files and **must run in order, one agent at a time**.

| Track | Tasks | Files owned |
|-------|-------|-------------|
| A (churn fix — land first) | 1, 2 | `frame_cache.py`, `test_frame_cache.py`, `camera.py` |
| B (session hygiene) | 3 → 4 → 5 → 6 | `stream_server.py`, `signaling.py`, `test_stream_server.py` |
| C (the record) | 8, 9 | design doc, `WHITEPAPER.md` |
| final | 7 | none (verification only) |

Task 7 runs after A and B complete. Track C only corrects prose and can run any time, but must describe the *target* behaviour of this plan, not the current code.

**Constants introduced, for reference:** `SNAPSHOT_TTL = 60.0` (`camera.py`), `COOLDOWN = 25.0`, `ANSWER_TIMEOUT = 6.0`, `RECENT_ANSWER = 15.0` (`stream_server.py`).

---

## Task 1: `FrameCache` — a TTL cache for stills

**Files:**
- Create: `custom_components/philips_avent/frame_cache.py`
- Create: `tests/test_philips_avent/test_frame_cache.py`

**Design decision (change 1), and why this shape:** the fix is `use_stream_for_stills = False` **plus** a TTL cache in front of the go2rtc frame path — not a cache behind `use_stream_for_stills = True`. When that property is True, HA core routes stills straight to the webrtc provider (`camera/__init__.py`: `_async_get_stream_image()` → `webrtc_provider.async_get_image()`) and the entity's `async_camera_image` is never consulted, so there is *no seam* for a cache. Turning the property off makes `async_camera_image` the single choke point we own: TTL throttling, stale-on-error, and single-flight all live there, while the actual pixels still come from go2rtc's frame path — no new media code. TTL is 60 s (the top of the 30–60 s range): the number that matters for the camera is concurrent slot occupancy, which Task 4's disconnect-on-replacement pins at ≤1 regardless of TTL, so we take the gentler churn (≤60 dials/hour worst case, down from 360, and only while something is actually requesting stills).

The cache logic goes in its own HA-free module because `camera.py` cannot be imported under bare pytest (it imports `homeassistant.*`; see `tests/test_philips_avent/conftest.py`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_philips_avent/test_frame_cache.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_frame_cache.py -v
```

Expected: `ModuleNotFoundError: No module named 'frame_cache'`.

- [ ] **Step 3: Write the implementation**

Create `custom_components/philips_avent/frame_cache.py`:

```python
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
```

Note the lock gives single-flight for free: concurrent callers queue, and all but the first see a fresh `_attempted_at` and return the cached frame. Note also `_attempted_at` is stamped *before* the fetch so a slow fetch does not extend its own window.

- [ ] **Step 4: Run test to verify it passes**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_frame_cache.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Full suite and lint**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
```

Expected: all PASS, no lint issues.

- [ ] **Step 6: Commit**

```bash
git add custom_components/philips_avent/frame_cache.py tests/test_philips_avent/test_frame_cache.py
git commit -m "feat(builtin): TTL cache for still frames"
```

---

## Task 2: Stop snapshots opening sessions (`camera.py`)

**Files:**
- Modify: `custom_components/philips_avent/camera.py`

Depends on Task 1. This is glue only — the logic is in `FrameCache`, already tested. `camera.py` cannot be unit-tested (it imports Home Assistant), so verification is the full suite staying green, lint, and a careful read.

- [ ] **Step 1: Replace `use_stream_for_stills` and correct its false docstring**

In `custom_components/philips_avent/camera.py`, replace the whole `use_stream_for_stills` property with:

```python
    @property
    def use_stream_for_stills(self) -> bool:
        """Never route stills through go2rtc's /api/frame.jpeg.

        go2rtc's frame handler (internal/mjpeg/mjpeg.go) does not share a
        running stream: it dials the producer, waits for one keyframe, and
        stops the producer again. With this property True, Home Assistant's
        10 s thumbnail poll therefore opened a complete Tuya session every
        10 seconds from an idle dashboard — enough to exhaust the camera's
        3-5 slot session pool and lock the vendor app out. Builtin stills
        are served from a TTL cache in async_camera_image instead; the
        add-on backend keeps its ffmpeg path, which its RTSP URL supports.
        """
        return False
```

The explicit `False` override stays (rather than deleting the property) so a future HA default can never silently flip this back on. During implementation, confirm against the installed HA (2026.8.x) that the base class default would indeed be routed through the provider when a webrtc provider is attached — the override is correct either way.

- [ ] **Step 2: Serve builtin stills from the cache**

Add the import and TTL constant near the top of `camera.py`:

```python
from .frame_cache import FrameCache

#: How long a dashboard thumbnail may lag reality. Attempts, not successes,
#: are throttled — see frame_cache.py. 60 s turns the 10 s poll's ~360
#: sessions/hour into at most 60, and Task 4's disconnect-on-replacement
#: keeps snapshot churn to at most one occupied pool slot either way.
SNAPSHOT_TTL = 60.0
```

In `AventCamera.__init__`, after `self._builtin = builtin`:

```python
        self._frame_cache = FrameCache(self._fetch_still, ttl=SNAPSHOT_TTL) if builtin else None
```

Add the fetch method (this is the go2rtc frame path, now behind the cache):

```python
    async def _fetch_still(self) -> bytes | None:
        """One frame via go2rtc's frame path; the cache calls this at most once per TTL."""
        if (provider := self._webrtc_provider) is None:
            return None
        return await provider.async_get_image(self)
```

Verify the exact attribute and method names (`_webrtc_provider`, `async_get_image`) against the installed `homeassistant` (2026.8.x) `components/camera/__init__.py` — the design doc verified `webrtc_provider.async_get_image()` exists in 2026.8.1, but the entity-side attribute name must be confirmed, and `async_get_image`'s signature (it accepts the camera plus optional width/height) matched. Width/height hints are deliberately not forwarded: the cache stores one native-size frame and the frontend scales thumbnails anyway.

- [ ] **Step 3: Branch `async_camera_image` and correct its docstring**

Replace `async_camera_image` with:

```python
    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """A single JPEG frame, required by the `camera.snapshot` service.

        Builtin backend: served from a TTL cache so the dashboard's 10 s
        still poll cannot open a Tuya session per poll (see
        use_stream_for_stills). The frame may be up to SNAPSHOT_TTL old.

        Add-on backend: unchanged — ffmpeg pulls one frame from the bridge's
        RTSP URL. The bridge fans its single Tuya session out to N RTSP
        clients, so there the snapshot genuinely does share the running
        stream and adds no load on the camera.
        """
        if self._frame_cache is not None:
            return await self._frame_cache.image()
        try:
            return await ffmpeg_get_image(
                self.hass, self._stream_url, width=width, height=height
            )
        except Exception:
            _LOGGER.exception("ffmpeg snapshot failed for %s", self._stream_url)
            return None
```

The add-on branch is byte-identical to today's body, including its existing exception handling; only the (true, for that backend) sharing claim is kept and scoped.

- [ ] **Step 4: Full suite and lint**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
```

Expected: all PASS (the suite never imports `camera.py`; this proves no collateral damage), no lint issues.

- [ ] **Step 5: Commit**

```bash
git add custom_components/philips_avent/camera.py
git commit -m "fix(builtin): stop the 10s thumbnail poll opening a Tuya session per poll"
```

---

## Task 3: Circuit breaker after an unanswered offer (`stream_server.py`)

**Files:**
- Modify: `custom_components/philips_avent/stream_server.py`
- Modify: `tests/test_philips_avent/test_stream_server.py` (append tests only in this task)

A camera that does not answer is almost always a camera whose session pool is full. Dialling again immediately — which go2rtc does, and doubly so via its `ffmpeg:` second source — burns another slot and makes the pool worse. After a no-answer timeout, refuse further dials for `COOLDOWN = 25.0` seconds **without making any Tuya cloud call or sending any offer**, so the pool can drain. 25 s sits in the requested 20–30 s band: long enough to outlast go2rtc's immediate retry burst, short enough that a genuinely recovered camera is reachable within half a minute.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_philips_avent/test_stream_server.py`:

```python
class TestCircuitBreaker:
    """After a no-answer timeout, the camera is left alone for COOLDOWN."""

    def test_a_timeout_starts_the_cooldown_and_the_next_dial_is_refused(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        async def go():
            with pytest.raises(SignalingError, match="did not answer"):
                await server._negotiate(source, OFFER, candidates.append)
            with pytest.raises(SignalingError, match="not dialling"):
                await server._negotiate(source, OFFER, candidates.append)

        run(go())
        # The refused dial made no cloud call and sent no offer.
        assert hub.opened == 1

    def test_the_cooldown_expires(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        monkeypatch.setattr(module, "COOLDOWN", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        async def go():
            with pytest.raises(SignalingError, match="did not answer"):
                await server._negotiate(source, OFFER, candidates.append)
            await asyncio.sleep(0.1)
            hub.answer = ANSWER  # the pool drained
            await server._negotiate(source, OFFER, candidates.append)

        run(go())
        assert hub.opened == 2

    def test_an_answer_clears_the_cooldown(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        monkeypatch.setattr(module, "COOLDOWN", 300.0)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        async def go():
            with pytest.raises(SignalingError, match="did not answer"):
                await server._negotiate(source, OFFER, candidates.append)
            source.cooldown_until = 0.0  # operator intervention / expiry
            hub.answer = ANSWER
            await server._negotiate(source, OFFER, candidates.append)
            # A successful answer must reset the breaker for the future.
            assert source.cooldown_until == 0.0

        run(go())

    def test_the_refusal_never_touches_a_live_stream(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, candidates.append)
            source.cooldown_until = asyncio.get_running_loop().time() + 30
            with pytest.raises(SignalingError, match="not dialling"):
                await server._negotiate(source, OFFER, candidates.append)
            return stream

        stream = run(go())
        # The check runs before the replacement release: the stream survives.
        assert not stream.released
        assert hub.opened == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_stream_server.py -k CircuitBreaker -v
```

Expected: FAIL — `CameraSource` has no `cooldown_until`, and no "not dialling" error is raised.

- [ ] **Step 3: Implement**

In `custom_components/philips_avent/stream_server.py`:

Add the constant next to `ANSWER_TIMEOUT`:

```python
#: After an unanswered offer, leave the camera alone this long. Its session
#: pool holds 3-5 slots and a full pool closes newcomers, so redialling —
#: which go2rtc does eagerly, twice per stream via its ffmpeg second source —
#: only digs the hole deeper. No cloud call, no offer, until this expires.
COOLDOWN = 25.0
```

Add to `CameraSource`:

```python
    cooldown_until: float = field(default=0.0, compare=False)
```

In `_negotiate`, as the *first* statements — before the previous stream is released and before `hub.open_session` (the cloud call):

```python
        loop = asyncio.get_running_loop()
        if (remaining := source.cooldown_until - loop.time()) > 0:
            raise SignalingError(
                f"{source.name} did not answer a recent offer; not dialling again "
                f"for another {remaining:.0f}s so its session pool can drain"
            )
```

In the `TimeoutError` branch, before raising:

```python
            source.cooldown_until = loop.time() + COOLDOWN
```

After a successful answer (next to the existing `_LOGGER.debug("%s answered ...")`):

```python
        source.cooldown_until = 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_stream_server.py -v
```

Expected: all PASS, new and old.

- [ ] **Step 5: Full suite, lint, commit**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
git add custom_components/philips_avent/stream_server.py tests/test_philips_avent/test_stream_server.py
git commit -m "feat(builtin): circuit breaker after an unanswered offer"
```

---

## Task 4: Restore `send_disconnect`, own session only

**Files:**
- Modify: `custom_components/philips_avent/signaling.py`
- Modify: `custom_components/philips_avent/stream_server.py`
- Modify: `tests/test_philips_avent/test_stream_server.py` (module docstring, fake, lifetime tests rewritten)

This corrects the wrong conclusion at its source. The rules:

- **Send `disconnect` when we abandon a session of our own:** answer timeout, handshake failure, and replacement by a new dial. This frees our slot in the camera's pool instead of leaving a zombie.
- **Skip it when the release reason is the camera's own `disconnect` frame** — it hung up first.
- **Stay silent where media may still be flowing:** linger expiry, camera unregistered (config entry unload), server stop.
- The method takes **no session-id parameter**, so a foreign id is impossible to express.

- [ ] **Step 1: Rewrite the test module docstring, the fake, and the lifetime tests**

In `tests/test_philips_avent/test_stream_server.py`:

Replace the module docstring with:

```python
"""Tests for the stream server's session lifetime.

The rules being guarded (established against go2rtc source, field-trial logs
and Tuya's own firmware docs — see
docs/superpowers/specs/2026-08-09-containerless-camera-design.md): the
camera's `disconnect` frame is *session-scoped*, and the camera's session
pool is small (3-5 slots) with slow zombie reclaim, so:

- we SEND `disconnect` — always for our own session id, the only one the
  API can express — when we abandon a session: answer timeout, handshake
  failure, replacement by a new dial;
- we STAY SILENT when the camera itself hung up, and whenever media may
  still be flowing: linger expiry, config entry unload, server stop.

(An earlier revision asserted the frame was peer-scoped and never sent it.
That observation was a misread, contaminated by a second go2rtc dialling
the same endpoint; it left zombie sessions the camera reclaimed only on
~20-minute timers.)
"""
```

In `FakeSession`, replace `send_disconnect` with the real method's shape — no parameter:

```python
    def send_disconnect(self) -> None:
        self.hub.disconnected.append(self.session_id)
```

Replace the whole `TestSessionLifetime` class with:

```python
class TestSessionLifetime:
    def test_replacing_a_stream_disconnects_the_replaced_session_only(self):
        """A redial frees the old slot; the new session is never touched."""
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, candidates.append)
            await server._negotiate(source, OFFER, candidates.append)

        run(go())
        assert hub.disconnected == ["session-1"]
        assert hub.sessions[0].closed
        assert not hub.sessions[1].closed
        assert len(server._streams) == 1

    def test_no_answer_disconnects_the_abandoned_session(self, monkeypatch):
        """The zombie fix: an unanswered offer must not hold a slot for ~20min."""
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        with pytest.raises(SignalingError, match="did not answer"):
            run(server._negotiate(source, OFFER, candidates.append))

        assert hub.disconnected == ["session-1"]
        assert server._streams == {}
        assert hub.sessions[0].closed

    def test_a_failed_handshake_disconnects_its_session(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            with pytest.raises(SdpError):
                await server._negotiate(source, "not sdp at all", candidates.append)

        run(go())
        assert hub.disconnected == ["session-1"]
        assert server._streams == {}

    def test_linger_expiry_stays_silent(self):
        """Media may still be flowing; only our own state is dropped."""
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, candidates.append)
            stream.release("linger expired")
            return stream

        stream = run(go())
        assert hub.disconnected == []
        assert stream.session.closed
        assert server._streams == {}

    def test_stopping_the_server_stays_silent(self):
        """Reloading a config entry must not black out a watched stream."""
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, candidates.append)
            await server.stop()

        run(go())
        assert hub.disconnected == []

    def test_the_cameras_own_disconnect_is_not_echoed(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, candidates.append)
            stream.session.on_disconnect()
            return stream

        stream = run(go())
        assert stream.released
        assert hub.disconnected == []  # it told us, not the other way round

    def test_unregistering_a_camera_stays_silent(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, candidates.append)
            server.remove_camera("cam1")

        run(go())
        assert server._streams == {}
        assert hub.disconnected == []
        assert server.cameras == {}

    def test_release_sends_at_most_one_disconnect(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, candidates.append)
            stream.release("no answer", disconnect=True)
            stream.release("no answer", disconnect=True)
            return stream

        run(go())
        assert hub.disconnected == ["session-1"]

    def test_hd_is_requested_once_the_stream_should_be_up(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "RESOLUTION_DELAY", 0.01)
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, candidates.append)
            await asyncio.sleep(0.05)
            return stream

        assert run(go()).session.resolutions == [0]
```

Notes for the implementer:

- `test_a_failed_handshake_disconnects_its_session` needs `SdpError` imported: add `from sdp import SdpError` next to the existing `from signaling import SignalingError`. It feeds a garbage offer so the failure happens *after* `open_session` — exactly the "handshake failure" abandonment case. If `rewrite_offer("not sdp at all", ...)` turns out not to raise `SdpError`, make it raise via a fixture offer that does; the point is any post-`open_session` failure, not the specific parser behaviour.
- The old `test_a_later_dial_never_disconnects_an_earlier_session` and `test_a_second_dial_replaces_the_first` are superseded by `test_replacing_a_stream_disconnects_the_replaced_session_only`; the old `test_release_never_disconnects_by_default` by `test_linger_expiry_stays_silent`; the old `test_a_failed_handshake_is_cleaned_up_silently` by its disconnecting counterpart. `TestNegotiation` keeps `test_no_answer_times_out_with_a_readable_message` but its silent-cleanup twin is gone.

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_stream_server.py -v
```

Expected: the rewritten lifetime tests FAIL (`hub.disconnected` stays empty; `release()` takes no `disconnect` kwarg).

- [ ] **Step 3: Add `send_disconnect` to `signaling.py`**

In `custom_components/philips_avent/signaling.py`:

Replace the frame-protocol comment block above `PROTOCOL_SESSION` with:

```python
# Frame protocol numbers, taken from the app's traffic.
PROTOCOL_SESSION = 302  # offer, answer, candidate, disconnect
PROTOCOL_CONTROL = 312  # resolution, speaker
```

Replace the `Session` class docstring with:

```python
    """One camera's offer/answer exchange.

    Closing a session stops us listening. Whether the camera is *told* is
    the caller's choice: `send_disconnect` is session-scoped and frees our
    slot in the camera's small session pool, so the stream server sends it
    when it abandons a session — and stays silent whenever media may still
    be flowing.
    """
```

Add the method next to `send_resolution`:

```python
    def send_disconnect(self) -> None:
        """Tell the camera this session is over, freeing its pool slot.

        Deliberately takes no session id: the frame always names our own
        session, so disconnecting someone else's is impossible to express.
        Matches the Go bridge (`bridge.go` `Stop()`) and go2rtc's own
        `pkg/tuya`, which both send it for their own session at teardown.
        """
        self._publish("disconnect", PROTOCOL_SESSION, {"mode": "webrtc"}, self.session_id)
```

(The body `{"mode": "webrtc"}` mirrors `DisconnectFrame` in `avent-webrtc-bridge/pkg/tuya/mqttCamera.go`.)

- [ ] **Step 4: Wire it into `stream_server.py`**

Change `Stream.release` to:

```python
    def release(self, reason: str, *, disconnect: bool = False) -> None:
        """Stop listening; optionally free our slot in the camera's pool.

        `disconnect=True` is for sessions we are abandoning — the camera
        would otherwise hold the slot as a zombie for many minutes. It must
        stay False when the camera itself hung up, and wherever media may
        still be flowing (linger, unload, server stop).
        """
        if self.released:
            return
        self.released = True
        for timer in self._timers:
            timer.cancel()
        _LOGGER.debug("Releasing stream for %s: %s", self.camera_id, reason)
        if disconnect:
            self.session.send_disconnect()
        self.session.close()
        self.server.forget(self)
```

Update exactly three call sites to pass `disconnect=True`:

- `previous.release("replaced by a new stream", disconnect=True)` in `_negotiate`;
- `stream.release("no answer", disconnect=True)` in the `TimeoutError` branch;
- `stream.release("handshake failed", disconnect=True)` in the `except Exception` branch.

All other call sites (`camera unregistered`, `server stopping`, `linger expired`, the `on_disconnect` lambda's `"camera disconnected"`) keep the default `False`. In `stop()`, replace the stale comment above the release with:

```python
            # Silent on purpose: reloading a config entry must not black out
            # a stream someone is watching. The linger state is ours alone.
```

- [ ] **Step 5: Rewrite the `stream_server.py` module docstring**

Replace everything from `### Why sessions linger` to the end of the docstring (including the whole "We never send the camera a `disconnect` frame" paragraph) with:

```
### The camera's session pool

TuyaOS IPC firmware serves a small fixed number of concurrent WebRTC
sessions (`max_client_num`, 3-5 on these models). A full pool makes the
camera forcibly close the *newcomer* — the vendor app's "Device Busy" — and
a session abandoned mid-handshake is not reclaimed promptly: it lingers as
a zombie until firmware timers fire, documented at ~20 minutes. Slots are
precious. The first field trial exhausted them from an idle dashboard,
because Home Assistant's 10 s still poll made go2rtc open a complete Tuya
session per thumbnail (~360/hour); camera.py now caches stills instead, and
this module refuses to redial a non-answering camera (COOLDOWN) so the pool
can drain.

### Session lifetime

go2rtc closes the websocket the moment ICE connects, so the socket says
nothing about whether anyone is still watching. Once the answer is relayed
we linger only long enough to hear a `disconnect` from a stream that dies
at birth, then drop our own state.

The `disconnect` frame is session-scoped: ids are per session, cameras send
per-session disconnects, and both go2rtc's pkg/tuya and this repo's Go
bridge send one for their own session id at their own teardown. (An earlier
revision concluded the frame was peer-scoped and removed it entirely; the
observation behind that was a misread — the "surviving" session had merely
been replaced by the next 10 s snapshot poll — and contaminated by a second
go2rtc dialling the same endpoint.) We send it exactly when we abandon a
session of our own: answer timeout, handshake failure, replacement by a new
dial. We stay silent when the camera itself hung up, and wherever media may
still be flowing: linger expiry, config entry unload, server stop.
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_stream_server.py -v
```

Expected: all PASS, including the circuit-breaker tests from Task 3 (their `hub.opened == 1` assertions are unaffected; the timeout case now also records one disconnect — adjust none of them, they don't assert on `disconnected`).

- [ ] **Step 7: Full suite, lint, commit**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
git add custom_components/philips_avent/signaling.py custom_components/philips_avent/stream_server.py tests/test_philips_avent/test_stream_server.py
git commit -m "fix(builtin): send disconnect for our own abandoned sessions"
```

---

## Task 5: `ANSWER_TIMEOUT` 20 s → 6 s

**Files:**
- Modify: `custom_components/philips_avent/stream_server.py`
- Modify: `tests/test_philips_avent/test_stream_server.py`

Only valid on top of Task 3: without the breaker, a shorter timeout just fails faster into go2rtc's retries and the `ffmpeg:` second source's double-dial. The camera answers in 74–135 ms when it answers at all; 6 s is ~50x that and still cuts the doomed-dial window by 70%.

- [ ] **Step 1: Write the failing test**

Append to `TestCircuitBreaker` in `tests/test_philips_avent/test_stream_server.py`:

```python
    def test_the_answer_timeout_is_short(self):
        """The camera answers in ~0.1s or not at all; holding a doomed dial
        open for 20s only delayed the breaker. Only valid with COOLDOWN."""
        import stream_server as module

        assert module.ANSWER_TIMEOUT == 6.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_stream_server.py -k timeout_is_short -v
```

Expected: FAIL (`20.0 != 6.0`).

- [ ] **Step 3: Change the constant**

In `stream_server.py`:

```python
#: Cameras answer in 74-135 ms when they answer at all; a dial still silent
#: after this is a full session pool, and COOLDOWN takes over from here.
ANSWER_TIMEOUT = 6.0
```

Also update the timeout's `SignalingError` message, whose parenthetical guess is now a verified fact — replace `"(offline, or still holding an earlier session?)"` with `"(offline, or its 3-5 slot session pool is full)"`.

- [ ] **Step 4: Verify, lint, commit**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
git add custom_components/philips_avent/stream_server.py tests/test_philips_avent/test_stream_server.py
git commit -m "fix(builtin): 6s answer timeout now the circuit breaker exists"
```

---

## Task 6: Guard the one-consumer invariant

**Files:**
- Modify: `custom_components/philips_avent/stream_server.py`
- Modify: `tests/test_philips_avent/test_stream_server.py`

go2rtc only redials when it has no producer, so a new websocket dialling a camera whose stream was answered *seconds* ago means a second go2rtc is attached — exactly the contamination that produced the wrong disconnect conclusion. This architecture serves one consumer.

**Decision: log loudly and proceed with the replacement, rather than refuse.** We cannot distinguish a second go2rtc from a legitimate quick reopen (user closes and reopens the dashboard; go2rtc drops its producer when the last viewer leaves), and refusing would strand a legitimate user with no stream at all. Since Task 4, a replacement sends a `disconnect` for the old session, so two competing go2rtc instances now fail *visibly* — each replacement kills the other's stream and the warning names the cause — instead of silently corrupting observations.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_philips_avent/test_stream_server.py`:

```python
class TestOneConsumer:
    def test_a_dial_soon_after_an_answer_is_flagged(self, caplog):
        """go2rtc only redials without a producer: this smells like a second one."""
        import logging

        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, candidates.append)
            with caplog.at_level(logging.WARNING, logger="stream_server"):
                await server._negotiate(source, OFFER, candidates.append)

        run(go())
        assert "second consumer" in caplog.text
        # Flagged, not refused: the replacement still happened.
        assert hub.opened == 2

    def test_replacing_an_unanswered_dial_is_not_flagged(self, monkeypatch, caplog):
        import logging

        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        monkeypatch.setattr(module, "COOLDOWN", 0.0)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        async def go():
            with pytest.raises(SignalingError):
                await server._negotiate(source, OFFER, candidates.append)
            hub.answer = ANSWER
            with caplog.at_level(logging.WARNING, logger="stream_server"):
                await server._negotiate(source, OFFER, candidates.append)

        run(go())
        assert "second consumer" not in caplog.text
```

Note the logger name: the tests import the module standalone, so `_LOGGER` is named `stream_server`, not `custom_components.philips_avent.stream_server`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/test_stream_server.py -k OneConsumer -v
```

Expected: FAIL (no warning emitted).

- [ ] **Step 3: Implement**

In `stream_server.py`, add the constant:

```python
#: A redial this soon after a successful answer means a second consumer is
#: attached (go2rtc only redials when it has no producer). One camera, one
#: consumer: two go2rtc instances will replace — and disconnect — each other.
RECENT_ANSWER = 15.0
```

In `Stream.__init__`, add `self.answered_at: float | None = None`. In `_negotiate`, set `stream.answered_at = loop.time()` right after the answer arrives (next to clearing the cooldown), and extend the replacement branch:

```python
        if (previous := self._streams.get(source.camera_id)) is not None:
            if previous.answered_at is not None and loop.time() - previous.answered_at < RECENT_ANSWER:
                _LOGGER.warning(
                    "A second consumer appears to be dialling %s: its stream was "
                    "answered only %.0fs ago and go2rtc redials only when it has no "
                    "producer. This backend serves exactly one consumer per camera; "
                    "two will keep disconnecting each other's sessions.",
                    source.name,
                    loop.time() - previous.answered_at,
                )
            previous.release("replaced by a new stream", disconnect=True)
```

- [ ] **Step 4: Verify, lint, commit**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
git add custom_components/philips_avent/stream_server.py tests/test_philips_avent/test_stream_server.py
git commit -m "feat(builtin): flag a second consumer dialling a freshly answered camera"
```

---

## Task 7: Full verification pass

**Files:** none (verification only). Runs after Tracks A and B are both complete.

- [ ] **Step 1: Full Python suite**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
```

Expected: all PASS — the frame-cache tests, the rewritten stream-server suite, and every pre-existing test.

- [ ] **Step 2: Lint with the CI command**

```bash
ruff check custom_components/ examples/ tools/ --ignore E501
```

Expected: no issues (note the `# noqa: BLE001 - <reason>` comments introduced follow the pinned rule set's convention).

- [ ] **Step 3: Grep for leftovers of the wrong conclusion**

```bash
grep -rn "never send\|peer link\|ends the camera's link\|end my link" custom_components/philips_avent/ tests/test_philips_avent/
```

Expected: no hits describing `disconnect` as peer-scoped. (Hits in git history are fine; hits in working-tree code or tests are not.)

- [ ] **Step 4: Confirm the HA-free modules stayed HA-free**

```bash
grep -n "homeassistant" custom_components/philips_avent/{sdp,signaling,stream_server,frame_cache}.py
```

Expected: no output.

No commit — this task is verification only.

---

## Task 8: Correct the design doc

**Files:**
- Modify: `docs/superpowers/specs/2026-08-09-containerless-camera-design.md`

The doc currently asserts conclusions the review refuted, and is missing new protocol knowledge worth recording. Keep the doc's voice: verified facts, dated, honest about what is and is not measured.

- [ ] **Step 1: Rewrite the session-lifetime paragraph in "What shipped"**

Replace the paragraph beginning `**Session lifetime — the answer to R8.**` with:

```markdown
**Session lifetime — the answer to R8, twice corrected.** After the answer is
relayed we hold nothing the camera needs; a session lingers 120 s (long
enough to hear a `disconnect` from a stream that dies at birth), then we
drop our own state. But "the camera reclaims its own slot promptly" was
wrong, and so was a first-field-trial conclusion that the `disconnect`
frame is peer-scoped and must never be sent. The verified mechanism
(2026-08-09, against go2rtc source, the trial logs and Tuya's TuyaOS IPC
firmware docs): the frame is *session-scoped* — ids are per session,
cameras send per-session disconnects, and both go2rtc's `pkg/tuya` and this
repo's Go bridge send it for their own session id at their own teardown.
The observation behind the peer-scoped conclusion was a misread (the
"surviving" session had merely been replaced by the next 10 s snapshot
poll) and contaminated (a second go2rtc was dialling the same endpoint).
The shipped rules: send `disconnect` — own session only, the API cannot
express another — on answer timeout, handshake failure and
replacement-by-redial; stay silent when the camera hung up first and
wherever media may still be flowing (linger expiry, config entry unload,
server stop). See
`docs/superpowers/plans/2026-08-09-builtin-backend-session-hygiene.md`.
```

- [ ] **Step 2: Add a field-trial section recording the new protocol knowledge**

Insert a new section after "What shipped" (before "Verified on hardware"):

```markdown
### First field trial (2026-08-09): the session pool

The first day of real use saturated the camera so badly the vendor app
could not connect. The mechanism, each link verified:

* **HA's thumbnail poll was the churn.** With `use_stream_for_stills =
  True` every still goes to go2rtc's `/api/frame.jpeg`, whose handler
  (`internal/mjpeg/mjpeg.go`) dials the producer, waits for one keyframe,
  then **stops the producer** — it does not share a running stream. Redial
  timestamps in the log were phase-locked to a 10.00 s period: an idle
  dashboard opened ~360 complete Tuya sessions per hour. Fixed by a 60 s
  TTL frame cache behind `use_stream_for_stills = False` (`frame_cache.py`).
* **The pool is 3–5 sessions** (`max_client_num`, TuyaOS IPC firmware).
  When full, the camera forcibly closes the *newcomer* — the vendor app's
  "Device Busy". Sessions abandoned mid-handshake become zombies reclaimed
  on timers documented at ~20 minutes, which is why recovery took ~10
  minutes and why the vendor app was locked out. This answers R5's ceiling
  and R8's "how long to reclaim a slot" measurement.
* **Answer latency is bimodal:** 74–135 ms when the camera answers at all;
  otherwise silence. `ANSWER_TIMEOUT` dropped from 20 s to 6 s, valid only
  alongside the cooldown: after an unanswered offer the server refuses to
  dial for 25 s — no cloud call, no offer — so the pool can drain instead
  of absorbing go2rtc's retries and its `ffmpeg:` second source's
  double-dial.
```

- [ ] **Step 3: Update R5 and R8**

In "Risks and open questions":

- **R5**: replace "Unknown ceiling — measure." with "Ceiling measured: 3–5 concurrent sessions (`max_client_num`); a full pool closes the newcomer. See 'First field trial'."
- **R8**: mark it closed. Replace the paragraph's final sentences (from "What remains is the measurement…") with: "Closed by the first field trial: reclaim of an abandoned slot is on ~20-minute zombie timers, far too slow to lean on — which is why abandoned sessions are now explicitly disconnected (own session id only) and unanswered cameras get a 25 s cooldown. See 'First field trial' and the session-hygiene plan."

- [ ] **Step 4: Update the status line**

Extend the `Status:` paragraph at the top with one sentence: "A first field trial (2026-08-09) exposed a session-pool exhaustion failure mode and refuted one conclusion previously recorded here; see 'First field trial' and `docs/superpowers/plans/2026-08-09-builtin-backend-session-hygiene.md`."

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-08-09-containerless-camera-design.md
git commit -m "docs(spec): correct the disconnect-scope conclusion, record the session pool"
```

---

## Task 9: WHITEPAPER subsection on the session pool

**Files:**
- Modify: `WHITEPAPER.md`

The pool size, the zombie reclaim and the disconnect scope are protocol knowledge on par with the "camera is picky about the offer" findings, and belong in the protocol reference — especially since the Go tree (whose comments carry some of this) is slated for deletion in the design doc's Phase 3.

- [ ] **Step 1: Add the subsection**

In section 14 ("Phase 8: Streaming Path — WebRTC Signaling and Media"), immediately after the subsection "The camera is picky about the offer it will answer", insert:

```markdown
### The session pool: `max_client_num`, zombies, and `disconnect` scope

*Established 2026-08-09 during the first field trial of the Python signaling
relay (see `docs/superpowers/specs/2026-08-09-containerless-camera-design.md`),
verified against the trial logs, go2rtc's source and Tuya's TuyaOS IPC
firmware documentation.*

The camera serves a small fixed number of concurrent WebRTC sessions —
`max_client_num` in TuyaOS IPC firmware, **3–5** on these models. Three
behaviours follow, none advertised in the API:

1. **A full pool closes the newcomer, not the oldest session.** The dial
   fails — the vendor app surfaces it as "Device Busy" — while established
   sessions keep streaming. There is no queueing and no eviction.
2. **Abandoned sessions become zombies.** A session whose handshake was
   started and never completed, or whose peer vanished without a
   `disconnect`, is reclaimed only by firmware timers, documented at
   ~20 minutes. A client that opens sessions faster than it disconnects
   them will lock everyone out of the camera, including the vendor app.
   (Home Assistant's 10 s thumbnail poll, routed through go2rtc's
   dial-grab-stop `/api/frame.jpeg` handler, opened ~360 sessions/hour and
   did exactly that.)
3. **The `disconnect` frame is session-scoped.** It carries the sender's
   `sessionid` (section 14's frame format) and ends that session only.
   Session ids are per-session; the camera sends per-session disconnects.
   Both go2rtc's `pkg/tuya` and this project's bridge
   (`pkg/rtsp/bridge.go`, `Stop()`) send it for their own session id at
   their own teardown, body `{"mode": "webrtc"}` on protocol 302. Sending
   it promptly for every session you abandon is what keeps the pool
   healthy; there is no way to free someone else's slot, and no need to.

When the pool has free slots the camera answers an offer in **74–135 ms**;
when it is full it answers with silence. Clients should treat a short
answer timeout (a few seconds) plus a back-off of tens of seconds as
protocol, not tuning.
```

- [ ] **Step 2: Check the table of contents**

`WHITEPAPER.md` has a Table of Contents near the top; it lists sections, not subsections — confirm, and update only if subsections of section 14 are listed there.

- [ ] **Step 3: Commit**

```bash
git add WHITEPAPER.md
git commit -m "docs(whitepaper): session pool, zombie reclaim and disconnect scope"
```

---

## Wrap-up

After Task 9 the work is done. Push when ready:

```bash
git push git@github.com:thekoma/aventproxy.git main
```

### Field verification (later, performed by the user — not by an agent, and not part of this plan)

The camera is a real baby monitor recovering from pool exhaustion; do not stream at it to test. When the user next runs this build, the log tells the story:

- **Churn gone:** with a dashboard open but nobody watching the stream, there must be **no** `Offering ... to <camera>` lines on a 10-second period. At most one per minute while stills are being requested, and none once the dashboard is closed.
- **Breaker working:** a genuinely busy camera produces `did not answer within 6s` followed by `not dialling again for another Ns` — and *no* cloud traffic or offers during those N seconds. The pair should never repeat more often than every ~25 s.
- **Slots being freed:** `Releasing stream ...: replaced by a new stream` / `no answer` / `handshake failed` lines are now accompanied by a disconnect frame on the wire; the observable symptom is that a redial after a failure starts working within seconds rather than after ~10–20 minutes.
- **The real prize:** the Philips Avent phone app connects promptly *while* Home Assistant has a live stream open. That is the failure that started all this; it is also the acceptance test.
- **Second-consumer guard:** the warning `A second consumer appears to be dialling ...` must not appear in normal operation. If it does, a stray go2rtc (or a second HA) is attached — find it before drawing any conclusions from the log, because that contamination is exactly what produced the wrong disconnect theory the first time.
