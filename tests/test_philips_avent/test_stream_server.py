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
from sdp import SdpError
from signaling import SignalingError
from stream_server import CameraSource, StreamServer

FIXTURES = Path(__file__).parent / "fixtures"
OFFER = (FIXTURES / "go2rtc-offer.sdp").read_text()
ANSWER = (FIXTURES / "camera-answer.sdp").read_text()

CANDIDATES = ["candidate:1 1 UDP 2130706431 192.168.1.97 49592 typ host"]


def run(coro):
    return asyncio.run(coro)


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

    async def open_session(self, camera_id: str, *, talkback: bool = False) -> FakeSession:
        if self.error is not None:
            raise self.error
        self.opened += 1
        session = FakeSession(camera_id, self)
        self.sessions.append(session)
        return session


candidates: list[str] = []


def build(hub: FakeHub, token: str = "secret") -> tuple[StreamServer, CameraSource]:
    candidates.clear()
    server = StreamServer(0)
    source = CameraSource(camera_id="cam1", name="Nursery", hub=hub, token=token)
    server.add_camera(source)
    return server, source


class TestNegotiation:
    def test_answer_is_rewritten_for_the_consumer(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            return await server._negotiate(source, OFFER, candidates.append)

        _stream, answer = run(go())
        # Three m-lines back for the three that go2rtc offered.
        assert answer.count("m=") == OFFER.count("m=")

    def test_camera_sees_an_audio_first_offer(self):
        hub = FakeHub()
        server, source = build(hub)
        run(server._negotiate(source, OFFER, candidates.append))
        sent = hub.sessions[0].offers[0]
        assert sent.index("m=audio") < sent.index("m=video")

    def test_candidates_are_relayed_to_the_consumer(self):
        hub = FakeHub()
        server, source = build(hub)

        async def go():
            await server._negotiate(source, OFFER, candidates.append)
            await asyncio.sleep(0)  # let the queued callbacks run

        run(go())
        assert candidates == CANDIDATES

    def test_no_answer_times_out_with_a_readable_message(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)

        with pytest.raises(SignalingError, match="did not answer"):
            run(server._negotiate(source, OFFER, candidates.append))

    def test_a_cloud_failure_is_reported_verbatim(self):
        hub = FakeHub(error=SignalingError("camera returned no signaling id"))
        server, source = build(hub)
        with pytest.raises(SignalingError, match="no signaling id"):
            run(server._negotiate(source, OFFER, candidates.append))


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


class TestAuthorization:
    class FakeRequest:
        def __init__(self, camera_id: str, token: str):
            self.match_info = {"camera_id": camera_id}
            self.query = {"t": token}

    def test_the_right_token_gets_the_camera(self):
        server, source = build(FakeHub())
        assert server._authorize(self.FakeRequest("cam1", "secret")) is source

    def test_a_wrong_token_is_refused(self):
        from aiohttp import web

        server, _ = build(FakeHub())
        with pytest.raises(web.HTTPForbidden):
            server._authorize(self.FakeRequest("cam1", "guess"))

    def test_an_unknown_camera_is_refused(self):
        from aiohttp import web

        server, _ = build(FakeHub())
        with pytest.raises(web.HTTPForbidden):
            server._authorize(self.FakeRequest("cam2", "secret"))

    def test_no_token_is_refused(self):
        from aiohttp import web

        server, _ = build(FakeHub())
        with pytest.raises(web.HTTPForbidden):
            server._authorize(self.FakeRequest("cam1", ""))


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

    def test_the_answer_timeout_is_short(self):
        """The camera answers in ~0.1s or not at all; holding a doomed dial
        open for 20s only delayed the breaker. Only valid with COOLDOWN."""
        import stream_server as module

        assert module.ANSWER_TIMEOUT == 6.0


class TestOnAnswered:
    """The hook the keep_stream_running option hangs off (see preload.py).

    It must fire exactly on success — the moment the consumer that dialled
    us is guaranteed to know the stream, so go2rtc's preload can be armed —
    and never on a refused or failed dial, which would arm a preload for a
    stream go2rtc may not have registered.
    """

    def test_fires_after_a_successful_negotiation(self):
        hub = FakeHub()
        server, source = build(hub)
        answered: list[bool] = []
        source.on_answered = lambda: answered.append(True)

        run(server._negotiate(source, OFFER, candidates.append))

        assert answered == [True]

    def test_fires_on_every_successful_negotiation(self):
        """Not once-only: a redial means the producer was down, which is
        exactly when a preload has stopped doing its job and needs re-arming."""
        hub = FakeHub()
        server, source = build(hub)
        answered: list[bool] = []
        source.on_answered = lambda: answered.append(True)

        async def go():
            await server._negotiate(source, OFFER, candidates.append)
            await server._negotiate(source, OFFER, candidates.append)

        run(go())
        assert answered == [True, True]

    def test_does_not_fire_on_an_answer_timeout(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)
        answered: list[bool] = []
        source.on_answered = lambda: answered.append(True)

        with pytest.raises(SignalingError, match="did not answer"):
            run(server._negotiate(source, OFFER, candidates.append))

        assert answered == []

    def test_does_not_fire_on_a_failed_handshake(self):
        hub = FakeHub()
        server, source = build(hub)
        answered: list[bool] = []
        source.on_answered = lambda: answered.append(True)

        async def go():
            with pytest.raises(SdpError):
                await server._negotiate(source, "not sdp at all", candidates.append)

        run(go())
        assert answered == []

    def test_does_not_fire_while_the_cooldown_holds(self, monkeypatch):
        import stream_server as module

        monkeypatch.setattr(module, "ANSWER_TIMEOUT", 0.05)
        hub = FakeHub(answer=None)
        server, source = build(hub)
        answered: list[bool] = []
        source.on_answered = lambda: answered.append(True)

        async def go():
            with pytest.raises(SignalingError, match="did not answer"):
                await server._negotiate(source, OFFER, candidates.append)
            with pytest.raises(SignalingError, match="not dialling"):
                await server._negotiate(source, OFFER, candidates.append)

        run(go())
        assert answered == []

    def test_the_hook_is_optional(self):
        """The default None must negotiate exactly as before."""
        hub = FakeHub()
        server, source = build(hub)
        assert source.on_answered is None
        _stream, answer = run(server._negotiate(source, OFFER, candidates.append))
        assert answer
