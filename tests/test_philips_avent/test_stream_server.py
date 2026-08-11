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
import asyncio
from pathlib import Path

import pytest
import stream_server as module
from sdp import SdpError
from signaling import SignalingError
from stream_server import CameraSource, StreamServer

FIXTURES = Path(__file__).parent / "fixtures"
OFFER = (FIXTURES / "go2rtc-offer.sdp").read_text()
ANSWER = (FIXTURES / "camera-answer.sdp").read_text()

CANDIDATES = ["candidate:1 1 UDP 2130706431 192.168.1.97 49592 typ host"]


def run(coro):
    return asyncio.run(coro)


def sink(_candidate: str) -> None:
    """An on_candidate for tests that do not care about candidates."""


@pytest.fixture
def fast_timeout(monkeypatch):
    monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)


class FakeSession:
    """Stands in for signaling.Session, without a broker."""

    def __init__(self, camera_id: str, hub: "FakeHub"):
        self.camera_id = camera_id
        self.hub = hub
        self.session_id = f"session-{hub.opened}"
        self.on_answer = None
        self.on_candidate = None
        self.on_disconnect = None
        self.closed = False
        self.offers: list[str] = []
        self.resolutions: list[int] = []

    def send_offer(self, sdp: str) -> None:
        self.offers.append(sdp)
        if self.hub.answer is None:
            return  # camera says nothing; the caller should time out
        loop = asyncio.get_running_loop()
        loop.call_soon(self.on_answer, self.hub.answer)
        for candidate in CANDIDATES:
            loop.call_soon(self.on_candidate, candidate)

    def send_candidate(self, candidate: str) -> None:
        self.hub.sent_candidates.append(candidate)

    def send_resolution(self, value: int = 0) -> None:
        self.resolutions.append(value)

    def send_disconnect(self) -> None:
        self.hub.disconnected.append(self.session_id)

    def close(self) -> None:
        self.closed = True


class FakeHub:
    def __init__(self, answer: str | None = ANSWER, error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.opened = 0
        self.sessions: list[FakeSession] = []
        self.disconnected: list[str] = []
        self.sent_candidates: list[str] = []

    async def open_session(self, camera_id: str) -> FakeSession:
        if self.error is not None:
            raise self.error
        self.opened += 1
        session = FakeSession(camera_id, self)
        self.sessions.append(session)
        return session


def build(hub: FakeHub, token: str = "secret") -> tuple[StreamServer, CameraSource]:
    server = StreamServer(0)
    source = CameraSource(camera_id="cam1", name="Nursery", hub=hub, token=token)
    server.add_camera(source)
    return server, source


def watch_answers(source: CameraSource) -> list[bool]:
    """Record every on_answered firing (the keep_stream_running hook)."""
    answered: list[bool] = []
    source.on_answered = lambda: answered.append(True)
    return answered


class TestNegotiation:
    def test_offer_and_answer_are_rewritten_and_candidates_relayed(self):
        hub = FakeHub()
        server, source = build(hub)
        relayed: list[str] = []

        async def go():
            _stream, answer = await server._negotiate(source, OFFER, relayed.append)
            await asyncio.sleep(0)  # let the queued callbacks run
            return answer

        answer = run(go())
        sent = hub.sessions[0].offers[0]
        # The camera must see audio first; the consumer gets its own shape back.
        assert sent.index("m=audio") < sent.index("m=video")
        assert answer.count("m=") == OFFER.count("m=")
        assert relayed == CANDIDATES


class TestSessionLifetime:
    def test_replacing_a_stream_disconnects_the_replaced_session_only(self):
        """A redial frees the old slot; the new session is never touched."""
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, sink)
            await server._negotiate(source, OFFER, sink)

        run(go())
        assert hub.disconnected == ["session-1"]
        assert hub.sessions[0].closed
        assert not hub.sessions[1].closed
        assert len(server._streams) == 1

    def test_no_answer_disconnects_the_abandoned_session(self, fast_timeout):
        """The zombie fix: an unanswered offer must not hold a slot for ~20min."""
        hub = FakeHub(answer=None)
        server, source = build(hub)
        answered = watch_answers(source)

        with pytest.raises(SignalingError, match="did not answer"):
            run(server._negotiate(source, OFFER, sink))

        assert hub.disconnected == ["session-1"]
        assert server._streams == {}
        assert hub.sessions[0].closed
        assert answered == []  # the keep_stream_running hook must not fire

    def test_a_failed_handshake_disconnects_its_session(self):
        """The camera answered, but with something we cannot hand to the
        consumer: a failed handshake, and the slot must be freed."""
        hub = FakeHub(answer="v=0\r\ns=-\r\n")  # no media sections at all
        server, source = build(hub)
        answered = watch_answers(source)

        async def go():
            with pytest.raises(SdpError):
                await server._negotiate(source, OFFER, sink)

        run(go())
        assert hub.disconnected == ["session-1"]
        assert server._streams == {}
        assert answered == []

    def test_linger_expiry_stays_silent(self):
        """Media may still be flowing; only our own state is dropped."""
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, sink)
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
            await server._negotiate(source, OFFER, sink)
            await server.stop()

        run(go())
        assert hub.disconnected == []

    def test_the_cameras_own_disconnect_is_not_echoed(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, sink)
            stream.session.on_disconnect()
            return stream

        stream = run(go())
        assert stream.released
        assert hub.disconnected == []  # it told us, not the other way round

    def test_unregistering_a_camera_stays_silent(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, sink)
            server.remove_camera("cam1")

        run(go())
        assert server._streams == {}
        assert hub.disconnected == []
        assert server.cameras == {}


class TestAuthorization:
    class FakeRequest:
        def __init__(self, camera_id: str, token: str):
            self.match_info = {"camera_id": camera_id}
            self.query = {"t": token}

    def test_only_the_right_token_gets_the_camera(self):
        from aiohttp import web

        server, source = build(FakeHub())
        assert server._authorize(self.FakeRequest("cam1", "secret")) is source
        for camera_id, token in [("cam1", "guess"), ("cam1", ""), ("cam2", "secret")]:
            with pytest.raises(web.HTTPForbidden):
                server._authorize(self.FakeRequest(camera_id, token))


class TestCircuitBreaker:
    """After a no-answer timeout, the camera is left alone for COOLDOWN."""

    def test_a_timeout_starts_the_cooldown_and_the_next_dial_is_refused(self, fast_timeout):
        hub = FakeHub(answer=None)
        server, source = build(hub)
        answered = watch_answers(source)

        async def go():
            with pytest.raises(SignalingError, match="did not answer"):
                await server._negotiate(source, OFFER, sink)
            with pytest.raises(SignalingError, match="not dialling"):
                await server._negotiate(source, OFFER, sink)

        run(go())
        # The refused dial made no cloud call, sent no offer and armed nothing.
        assert hub.opened == 1
        assert answered == []

    def test_the_cooldown_expires_and_success_resets_the_breaker(self, fast_timeout, monkeypatch):
        monkeypatch.setattr(module, "COOLDOWN", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        async def go():
            with pytest.raises(SignalingError, match="did not answer"):
                await server._negotiate(source, OFFER, sink)
            await asyncio.sleep(0.1)
            hub.answer = ANSWER  # the pool drained
            await server._negotiate(source, OFFER, sink)
            # A successful answer must reset the breaker for the future.
            assert source.cooldown_until == 0.0
            assert source.refusals == 0

        run(go())
        assert hub.opened == 2

    def test_consecutive_refusals_grow_the_cooldown_exponentially(self, fast_timeout, monkeypatch):
        """The camera reclaims zombie slots over ~20 minutes; a fixed 25s
        redial against a full pool plants a fresh zombie per try and the
        pool never drains (field, 2026-08-11 10:59). The ladder doubles
        per refusal and caps at COOLDOWN_MAX."""
        monkeypatch.setattr(module, "COOLDOWN", 0.01)
        monkeypatch.setattr(module, "COOLDOWN_MAX", 0.04)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        async def go():
            loop = asyncio.get_running_loop()
            waits = []
            for _ in range(4):
                with pytest.raises(SignalingError, match="did not answer"):
                    await server._negotiate(source, OFFER, sink)
                waits.append(source.cooldown_until - loop.time())
                source.cooldown_until = 0.0  # let the next dial through now
            return waits

        waits = run(go())
        # 0.01, 0.02, 0.04, then capped at 0.04.
        assert waits[0] < waits[1] < waits[2]
        assert waits[3] == pytest.approx(waits[2], abs=0.005)
        assert source.refusals == 4

    def test_the_refusal_never_touches_a_live_stream(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            stream, _ = await server._negotiate(source, OFFER, sink)
            source.cooldown_until = asyncio.get_running_loop().time() + 30
            with pytest.raises(SignalingError, match="not dialling"):
                await server._negotiate(source, OFFER, sink)
            return stream

        stream = run(go())
        # The check runs before the replacement release: the stream survives.
        assert not stream.released
        assert hub.opened == 1


class TestOnAnswered:
    """The hook the keep_stream_running option hangs off (see preload.py).

    It must fire exactly on success — the moment the consumer that dialled
    us is guaranteed to know the stream, so go2rtc's preload can be armed —
    and never on a refused or failed dial (asserted alongside each failure
    scenario above), which would arm a preload for a stream go2rtc may not
    have registered.
    """

    def test_fires_on_every_successful_negotiation(self):
        """Not once-only: a redial means the producer was down, which is
        exactly when a preload has stopped doing its job and needs re-arming."""
        server, source = build(FakeHub())
        answered = watch_answers(source)

        async def go():
            await server._negotiate(source, OFFER, sink)
            await server._negotiate(source, OFFER, sink)

        run(go())
        assert answered == [True, True]
