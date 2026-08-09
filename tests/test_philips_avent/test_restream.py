"""Tests for the go2rtc restream glue (restream.py).

The rules being guarded (the why is in restream.py's module docstring):
stream_source()'s result is frozen into HA's cached Stream, so only a
permanent, config-shaped verdict may pick the webrtc: fallback — transient
trouble must still yield the stable RTSP URL. And `PUT /api/streams`
silently replaces a live stream, so registration checks before it PUTs.
"""
import asyncio
import logging
from functools import partial

import preload as preload_mod
import restream as restream_mod
from const import builtin_stream_url
from restream import MANAGED_RTSP, Restreamer, parse_rtsp_endpoint

WS = "webrtc:ws://127.0.0.1:38555/avent/cam1?t=tok"
SRC = "philips_avent_cam1_src"
WANT = (WS, f"ffmpeg:{SRC}#audio=aac")
RTSP = f"rtsp://127.0.0.1:18554/{SRC}?video&audio=aac"


def run(coro):
    return asyncio.run(coro)


def make_restreamer(hass):
    return Restreamer(hass, partial(builtin_stream_url, 38555, "tok"))


class FakeHass:
    def __init__(self):
        self.data = {}


class FakeState:
    """Shared backend state across client and session instantiations."""

    def __init__(self):
        self.streams: dict[str, FakeStream] = {}
        self.calls: list[tuple] = []
        self.rtsp_listen: str | None = "127.0.0.1:18554"
        self.probe_calls = 0
        self.fail_probe_with: BaseException | None = None
        self.fail_add_with: BaseException | None = None
        self.fail_whep_with: BaseException | None = None
        self.fail_snapshot_with: BaseException | None = None


class FakeProducer:
    def __init__(self, url):
        self.url = url


class FakeStream:
    def __init__(self, urls):
        self.producers = [FakeProducer(u) for u in urls]


class FakeStreamsAPI:
    def __init__(self, state):
        self._state = state

    async def list(self):
        self._state.calls.append(("streams.list",))
        return dict(self._state.streams)

    async def add(self, name, sources):
        self._state.calls.append(("streams.add", name, tuple(sources)))
        if self._state.fail_add_with is not None:
            raise self._state.fail_add_with
        self._state.streams[name] = FakeStream(list(sources))


class FakeSdpModel:
    """Mirrors go2rtc_client's WebRTCSdpOffer/WebRTCSdpAnswer: a model
    carrying `.sdp`, not a bare string — the real forward_whep_sdp_offer
    takes and returns models."""

    def __init__(self, sdp):
        self.sdp = sdp


class FakeWebRTCAPI:
    def __init__(self, state):
        self._state = state

    async def forward_whep_sdp_offer(self, source_name, offer):
        self._state.calls.append(("webrtc.whep", source_name, offer.sdp))
        if self._state.fail_whep_with is not None:
            raise self._state.fail_whep_with
        return FakeSdpModel("v=0 fake-answer")


class FakeResponse:
    def __init__(self, state):
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return {"rtsp": {"listen": self._state.rtsp_listen}}


class FakeSession:
    """Duck-types the one aiohttp call the probe makes."""

    def __init__(self, state):
        self._state = state

    def get(self, url):
        self._state.probe_calls += 1
        if self._state.fail_probe_with is not None:
            raise self._state.fail_probe_with
        return FakeResponse(self._state)


def install_go2rtc(monkeypatch, hass, state, url="http://localhost:11984/"):
    """Wire a fake Go2RtcRestClient and hass.data['go2rtc'], like test_preload."""

    class FakeConfig:
        pass

    FakeConfig.url = url
    FakeConfig.session = FakeSession(state)

    class FakeClient:
        def __init__(self, session, url):
            assert session is not None and url
            self.streams = FakeStreamsAPI(state)
            self.webrtc = FakeWebRTCAPI(state)

        async def get_jpeg_snapshot(self, name, width=None, height=None):
            # Mirrors the real top-level method (GET /api/frame.jpeg).
            state.calls.append(("frame.jpeg", name))
            if state.fail_snapshot_with is not None:
                raise state.fail_snapshot_with
            return b"\xff\xd8fake-jpeg"

    monkeypatch.setattr(preload_mod, "Go2RtcRestClient", FakeClient)
    monkeypatch.setattr(restream_mod, "WebRTCSdpOffer", FakeSdpModel)
    hass.data["go2rtc"] = FakeConfig()


def adds(state):
    return [c for c in state.calls if c[0] == "streams.add"]


# -- the decision table (pure) ---------------------------------------------


def test_managed_instance_loopback_rtsp():
    assert parse_rtsp_endpoint("127.0.0.1:18554", "localhost") == ("127.0.0.1", 18554)


def test_all_interfaces_on_local_api_host():
    assert parse_rtsp_endpoint(":8554", "127.0.0.1") == ("127.0.0.1", 8554)


def test_all_interfaces_on_remote_api_host():
    assert parse_rtsp_endpoint(":8554", "192.168.1.2") == ("192.168.1.2", 8554)


def test_remote_host_with_loopback_rtsp_is_unusable():
    # go2rtc could dial itself, but HA's PyAV could not reach it — and
    # stream_source() feeds both, so this must degrade to the ws URL.
    assert parse_rtsp_endpoint("127.0.0.1:8554", "192.168.1.2") is None


def test_bracketed_ipv6_loopback_is_loopback():
    # rpartition(":") leaves the host as "[::1]", which must not fall
    # through to the treat-as-remote branch.
    assert parse_rtsp_endpoint("[::1]:8554", "localhost") == ("127.0.0.1", 8554)
    assert parse_rtsp_endpoint("[::1]:8554", "192.168.1.2") is None


def test_absent_empty_or_garbage_listen():
    for listen in (None, "", ":", "nonsense", ":notaport"):
        assert parse_rtsp_endpoint(listen, "localhost") is None


# -- stream_url: probe, register, decide -----------------------------------


def test_rtsp_url_when_probe_and_registration_succeed(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert adds(state) == [("streams.add", SRC, WANT)]


def test_ws_fallback_when_no_go2rtc(monkeypatch, caplog):
    hass = FakeHass()  # hass.data empty: no go2rtc at all
    restreamer = make_restreamer(hass)
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(restreamer.stream_url("cam1")) == WS
        assert run(restreamer.stream_url("cam1")) == WS  # rechecked, not cached
    assert caplog.text.count("go2rtc") == 1  # complained once


def test_ws_fallback_when_rtsp_permanently_unusable(monkeypatch, caplog):
    """Only a config-shaped verdict may pick the ws URL: HA's
    Camera.async_create_stream calls stream_source() at most once per
    entity lifetime and freezes the result into its cached Stream."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.rtsp_listen = ""  # probe SUCCEEDS: RTSP is disabled
    restreamer = make_restreamer(hass)
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(restreamer.stream_url("cam1")) == WS
        assert run(restreamer.stream_url("cam1")) == WS
    assert "HLS" in caplog.text
    assert state.probe_calls == 1  # the verdict is cached


def test_transient_probe_failure_still_returns_rtsp_url(monkeypatch):
    """A ws URL here would freeze a dead fallback into the Stream until
    entry reload; the RTSP URL is stable and the stream worker retries."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_probe_with = TimeoutError()
    host, port = MANAGED_RTSP
    url = run(make_restreamer(hass).stream_url("cam1"))
    assert url == f"rtsp://{host}:{port}/{SRC}?video&audio=aac"


def test_transient_probe_failure_on_remote_host_returns_ws(monkeypatch):
    # Cannot guess a remote endpoint; remote go2rtc is unsupported anyway.
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state, url="http://192.168.1.2:1984/")
    state.fail_probe_with = TimeoutError()
    assert run(make_restreamer(hass).stream_url("cam1")) == WS


def test_no_put_when_already_registered_with_matching_sources(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(list(WANT))
    restreamer = make_restreamer(hass)
    run(restreamer.stream_url("cam1"))
    run(restreamer.stream_url("cam1"))
    assert adds(state) == []


def test_reput_when_sources_differ(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    stale = WS.replace("t=tok", "t=old")
    state.streams[SRC] = FakeStream([stale, WANT[1]])
    run(make_restreamer(hass).stream_url("cam1"))
    assert adds(state) == [("streams.add", SRC, WANT)]


def test_reregisters_after_go2rtc_restart(monkeypatch):
    """The callers that make this self-healing are the WHEP override (per
    live-view open), snapshot (per frame-cache miss) and the setup pass —
    NOT the stream component, which never re-calls stream_source(); and
    on_answered can never fire while _src is unregistered."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    restreamer = make_restreamer(hass)
    run(restreamer.stream_url("cam1"))
    state.streams.clear()  # watchdog respawn: registrations are gone
    run(restreamer.stream_url("cam1"))
    assert adds(state) == [("streams.add", SRC, WANT)] * 2


def test_concurrent_calls_register_once(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    restreamer = make_restreamer(hass)

    async def burst():
        await asyncio.gather(*(restreamer.stream_url("cam1") for _ in range(5)))

    run(burst())
    assert adds(state) == [("streams.add", SRC, WANT)]


def test_registration_failure_still_returns_rtsp_url(monkeypatch, caplog):
    """The URL is stable and HA's stream worker retries its source; the
    stream starts working the moment a later call registers _src."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_add_with = TimeoutError()
    restreamer = make_restreamer(hass)
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(restreamer.stream_url("cam1")) == RTSP
        assert run(restreamer.stream_url("cam1")) == RTSP
    assert caplog.text.count("TimeoutError") == 1  # complained once


# -- whep_answer: single-hop live view --------------------------------------


def test_whep_answer_negotiates_the_producer(monkeypatch):
    """The offer goes to the _src stream (one hop, native PCMU), as a model
    carrying the SDP; registration is ensured first so a fresh go2rtc can
    answer."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    answer = run(make_restreamer(hass).whep_answer("cam1", "v=0 offer"))
    assert answer == "v=0 fake-answer"
    assert ("webrtc.whep", SRC, "v=0 offer") in state.calls
    assert adds(state) == [("streams.add", SRC, WANT)]  # registered before the offer


def test_whep_answer_none_when_no_go2rtc(monkeypatch):
    hass = FakeHass()  # hass.data empty
    assert run(make_restreamer(hass).whep_answer("cam1", "v=0 offer")) is None


def test_whep_answer_never_raises(monkeypatch, caplog):
    """None means the camera sends the frontend a WebRTCError — a native
    entity has no provider to fall back on, so the failure must arrive as
    a message, never as an exception out of the offer handler."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_whep_with = TimeoutError()
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(make_restreamer(hass).whep_answer("cam1", "v=0 offer")) is None
    assert "TimeoutError" in caplog.text


def test_snapshot_grabs_a_frame_from_the_producer(monkeypatch):
    """Stills come from go2rtc's frame handler on _src — registered first,
    so a freshly restarted go2rtc can serve the very first thumbnail."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    frame = run(make_restreamer(hass).snapshot("cam1"))
    assert frame == b"\xff\xd8fake-jpeg"
    assert ("frame.jpeg", SRC) in state.calls
    assert adds(state) == [("streams.add", SRC, WANT)]  # registered before the grab


def test_snapshot_failure_returns_none(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_snapshot_with = TimeoutError()
    assert run(make_restreamer(hass).snapshot("cam1")) is None


def test_snapshot_none_when_no_go2rtc(monkeypatch):
    hass = FakeHass()  # hass.data empty
    assert run(make_restreamer(hass).snapshot("cam1")) is None


def test_probe_success_is_cached_transient_failure_is_not(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    restreamer = make_restreamer(hass)
    run(restreamer.stream_url("cam1"))
    run(restreamer.stream_url("cam1"))
    assert state.probe_calls == 1  # success: cached for the entry's lifetime

    hass2, state2 = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass2, state2)
    state2.fail_probe_with = TimeoutError()
    restreamer2 = make_restreamer(hass2)
    run(restreamer2.stream_url("cam1"))
    state2.fail_probe_with = None  # go2rtc came back
    run(restreamer2.stream_url("cam1"))
    assert state2.probe_calls == 2  # provisional endpoint was not cached
