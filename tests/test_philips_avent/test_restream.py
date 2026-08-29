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
AAC = "philips_avent_cam1_src_aac"
LIVE = "philips_avent_cam1_src_live"
FF = f"ffmpeg:{SRC}#video=copy#audio=aac"
FF_LIVE = f"ffmpeg:{SRC}#video=copy#audio=copy#async"
WANT_SRC = (WS,)
WANT_AAC = (FF,)
WANT_LIVE = (FF_LIVE,)
#: One registration pass registers all three streams, producer first.
BOTH = [
    ("streams.add", SRC, WANT_SRC),
    ("streams.add", AAC, WANT_AAC),
    ("streams.add", LIVE, WANT_LIVE),
]
RTSP = f"rtsp://127.0.0.1:18554/{AAC}"


def run(coro):
    return asyncio.run(coro)


def make_restreamer(hass):
    return Restreamer(hass, partial(builtin_stream_url, 38555, "tok"))


class FakeHass:
    def __init__(self):
        self.data = {}
        self.tasks = []

    def async_create_background_task(self, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


class FakeState:
    """Shared backend state across client and session instantiations."""

    def __init__(self):
        self.streams: dict[str, FakeStream] = {}
        self.preloads: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.rtsp_listen: str | None = "127.0.0.1:18554"
        self.probe_status = 200
        self.probe_calls = 0
        #: Enabling a preload starts the stream's producer, so subsequent
        #: streams.list() reports it in resolved/active form — the go2rtc
        #: behavior the warm-up polls for. Off for tests of the timeout.
        self.activate_on_preload = True
        self.fail_probe_with: BaseException | None = None
        self.fail_add_with: BaseException | None = None
        self.fail_preload_with: BaseException | None = None
        self.fail_whep_with: BaseException | None = None
        #: Stream names whose WHEP negotiation fails (the rest succeed).
        self.fail_whep_for: set[str] = set()
        self.fail_snapshot_with: BaseException | None = None
        #: Fail this many frame grabs before succeeding — the frame
        #: handler's coin-flip against a ~4 s GOP.
        self.fail_snapshot_times = 0
        #: Override for /api/streams?src= media kinds. None derives them
        #: from the stream's activity (active => both kinds, like a chain
        #: whose tracks are all up). A list is consumed one entry per
        #: poll, holding the last — ffmpeg registering video before AAC.
        self.kinds_sequence: list[list[str]] | None = None

    def media_kinds(self, name) -> list[str]:
        if self.kinds_sequence is not None:
            if len(self.kinds_sequence) > 1:
                return self.kinds_sequence.pop(0)
            return self.kinds_sequence[0]
        stream = self.streams.get(name)
        if stream and any(
            not p.url.startswith(("webrtc:", "ffmpeg:")) for p in stream.producers
        ):
            return ["video", "audio"]
        return []


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


class FakePreloadAPI:
    def __init__(self, state):
        self._state = state

    async def list(self):
        self._state.calls.append(("preload.list",))
        return dict(self._state.preloads)

    async def enable(self, name):
        self._state.calls.append(("preload.enable", name))
        if self._state.fail_preload_with is not None:
            raise self._state.fail_preload_with
        self._state.preloads[name] = {}
        if self._state.activate_on_preload and name in self._state.streams:
            self._state.streams[name] = FakeStream([f"exec:running {name}"])

    async def disable(self, name):
        self._state.calls.append(("preload.disable", name))
        self._state.preloads.pop(name, None)


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
        if self._state.fail_whep_with is not None or source_name in self._state.fail_whep_for:
            raise self._state.fail_whep_with or RuntimeError("no such stream")
        return FakeSdpModel("v=0 fake-answer")


class FakeResponse:
    def __init__(self, state):
        self._state = state
        self.status = state.probe_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return {"rtsp": {"listen": self._state.rtsp_listen}}


class FakeDetailResponse:
    """`GET /api/streams?src=` — the raw call _media_kinds makes."""

    def __init__(self, state, name):
        self._state = state
        self._name = name
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        kinds = self._state.media_kinds(self._name)
        self._state.calls.append(("streams.detail", self._name, tuple(kinds)))
        medias = [f"{kind}, recvonly, X" for kind in kinds]
        return {"producers": [{"url": "exec:running", "medias": medias}]}


class FakeSession:
    """Duck-types the two raw aiohttp calls: the /api probe and the
    /api/streams?src= track detail."""

    def __init__(self, state):
        self._state = state

    def get(self, url, params=None):
        if params and "src" in params:
            return FakeDetailResponse(self._state, params["src"])
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
            self.preload = FakePreloadAPI(state)

        async def get_jpeg_snapshot(self, name, width=None, height=None):
            # Mirrors the real top-level method (GET /api/frame.jpeg).
            state.calls.append(("frame.jpeg", name))
            if state.fail_snapshot_times > 0:
                state.fail_snapshot_times -= 1
                raise TimeoutError("no keyframe within the handler's patience")
            if state.fail_snapshot_with is not None:
                raise state.fail_snapshot_with
            return b"\xff\xd8fake-jpeg"

    monkeypatch.setattr(preload_mod, "Go2RtcRestClient", FakeClient)
    monkeypatch.setattr(restream_mod, "WebRTCSdpOffer", FakeSdpModel)
    # Fast warm-up bounds: the fakes activate synchronously (or never), so
    # the polls only need to be long enough to actually yield the loop.
    monkeypatch.setattr(restream_mod, "_WARMUP_TIMEOUT", 0.05)
    monkeypatch.setattr(restream_mod, "_WARMUP_POLL", 0.01)
    monkeypatch.setattr(restream_mod, "_WARMUP_SETTLE", 0.01)
    monkeypatch.setattr(restream_mod, "_WARMUP_RELEASE", 0.02)
    hass.data["go2rtc"] = FakeConfig()


def adds(state):
    return [c for c in state.calls if c[0] == "streams.add"]


def preload_calls(state):
    return [c for c in state.calls if c[0].startswith("preload.")]


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
    assert adds(state) == BOTH


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
    assert url == f"rtsp://{host}:{port}/{AAC}"


def test_transient_probe_failure_on_remote_host_returns_ws(monkeypatch):
    # Cannot guess a remote endpoint; remote go2rtc is unsupported anyway.
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state, url="http://192.168.1.2:1984/")
    state.fail_probe_with = TimeoutError()
    assert run(make_restreamer(hass).stream_url("cam1")) == WS


def test_no_put_when_already_registered_with_matching_sources(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(list(WANT_SRC))
    state.streams[AAC] = FakeStream(list(WANT_AAC))
    state.streams[LIVE] = FakeStream(list(WANT_LIVE))
    restreamer = make_restreamer(hass)
    run(restreamer.stream_url("cam1"))
    run(restreamer.stream_url("cam1"))
    assert adds(state) == []


def test_reput_when_sources_differ(monkeypatch):
    """Token rotation, seen while the stream is IDLE: every reported url is
    in configured form, none is our ws URL — genuinely stale, re-PUT. The
    untouched `_src_aac` is left alone."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    stale = WS.replace("t=tok", "t=old")
    state.streams[SRC] = FakeStream([stale])
    state.streams[AAC] = FakeStream(list(WANT_AAC))
    state.streams[LIVE] = FakeStream(list(WANT_LIVE))
    run(make_restreamer(hass).stream_url("cam1"))
    assert adds(state) == [("streams.add", SRC, WANT_SRC)]


#: How go2rtc reports the producers of RUNNING streams: an active producer
#: delegates serialization to its connection, so the ffmpeg source comes
#: back as the expanded exec command line and the ws source as whatever its
#: connection renders — neither equals the configured source string.
ACTIVE_SRC = ["ws://127.0.0.1:38555/avent/cam1"]
ACTIVE_AAC = [f"exec:ffmpeg -hide_banner -re -i rtsp://127.0.0.1:18554/{SRC} -c:v copy -c:a aac ..."]
ACTIVE_LIVE = [f"exec:ffmpeg -use_wallclock_as_timestamps 1 -i rtsp://127.0.0.1:18554/{SRC} -c copy ..."]


def test_no_reput_while_producers_are_active(monkeypatch):
    """THE storm bug (field, 2026-08-10): active producers serialize in
    resolved form, so a naive url compare mismatches exactly while the
    stream is running — and every re-PUT orphans live producers whose
    retry workers then churn Tuya sessions forever. Unrecognizable urls
    mean running, not wrong: skip."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(ACTIVE_SRC)
    state.streams[AAC] = FakeStream(ACTIVE_AAC)
    state.streams[LIVE] = FakeStream(ACTIVE_LIVE)
    restreamer = make_restreamer(hass)
    run(restreamer.stream_url("cam1"))
    run(restreamer.stream_url("cam1"))
    assert adds(state) == []


def test_no_reput_on_token_change_while_active(monkeypatch):
    """Even a genuine config change must not re-PUT over running producers:
    we cannot tell a resolved url apart from a stale one, and a wrong skip
    merely lasts until the next entry reload, while a wrong re-PUT is the
    session storm. Skip wins."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(ACTIVE_SRC)
    state.streams[AAC] = FakeStream(ACTIVE_AAC)
    state.streams[LIVE] = FakeStream(ACTIVE_LIVE)
    restreamer = Restreamer(hass, partial(builtin_stream_url, 38555, "rotated"))
    run(restreamer.stream_url("cam1"))
    assert adds(state) == []


def test_aac_stream_repairs_independently(monkeypatch):
    """A stale idle `_src_aac` re-PUTs alone; the producer — and the Tuya
    session behind it — is never touched for a recording-stream repair."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(list(WANT_SRC))
    state.streams[AAC] = FakeStream(["ffmpeg:oldshape#audio=opus"])
    state.streams[LIVE] = FakeStream(list(WANT_LIVE))
    run(make_restreamer(hass).stream_url("cam1"))
    assert adds(state) == [("streams.add", AAC, WANT_AAC)]


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
    assert adds(state) == BOTH + BOTH


def test_concurrent_calls_register_once(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    restreamer = make_restreamer(hass)

    async def burst():
        await asyncio.gather(*(restreamer.stream_url("cam1") for _ in range(5)))

    run(burst())
    assert adds(state) == BOTH


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


def test_whep_answer_negotiates_the_rebased_stream(monkeypatch):
    """The offer goes to _src_live, whose ffmpeg hop rebases this camera's
    frozen video clock (measured 2026-08-29: every _src video packet at pts
    0.000000 while its own audio advanced). Registration is ensured first
    so a fresh go2rtc can answer."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    answer = run(make_restreamer(hass).whep_answer("cam1", "v=0 offer"))
    assert answer == "v=0 fake-answer"
    assert ("webrtc.whep", LIVE, "v=0 offer") in state.calls
    assert ("webrtc.whep", SRC, "v=0 offer") not in state.calls  # no needless fallback
    assert adds(state) == BOTH  # registered before the offer


def test_whep_falls_back_to_the_raw_producer(monkeypatch):
    """If the rebased stream cannot answer, live view must still work: the
    fallback IS the pre-2026-08-29 behaviour, so the worst case of the
    extra hop is the live view we already had, never a black card."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_whep_for = {LIVE}
    answer = run(make_restreamer(hass).whep_answer("cam1", "v=0 offer"))
    assert answer == "v=0 fake-answer"
    assert ("webrtc.whep", SRC, "v=0 offer") in state.calls


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


def test_probe_http_4xx_is_a_permanent_verdict(monkeypatch, caplog):
    """A 4xx is config-shaped — this go2rtc will keep refusing (auth we do
    not have, a path ACL) — so it caches the ws fallback like no-RTSP,
    instead of pinning MANAGED_RTSP on a possibly-external instance
    forever."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.probe_status = 404
    restreamer = make_restreamer(hass)
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(restreamer.stream_url("cam1")) == WS
        assert run(restreamer.stream_url("cam1")) == WS
    assert "HTTP 404" in caplog.text
    assert state.probe_calls == 1  # the verdict is cached


def test_probe_http_5xx_is_transient(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.probe_status = 503
    restreamer = make_restreamer(hass)
    host, port = MANAGED_RTSP
    assert run(restreamer.stream_url("cam1")) == f"rtsp://{host}:{port}/{AAC}"
    run(restreamer.stream_url("cam1"))
    assert state.probe_calls == 2  # nothing cached, re-probed


def test_registration_error_never_leaks_the_token(monkeypatch, caplog):
    """A failed PUT renders its request URL, whose query carries the ws
    source with the stream token; describe_error redacts query strings."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_add_with = RuntimeError(
        "400, url='http://localhost:11984/api/streams?name=x&src=webrtc%3A"
        "ws%3A%2F%2F127.0.0.1%3A38555%2Favent%2Fcam1%3Ft%3Dtok'"
    )
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert "Could not register" in caplog.text
    assert "tok" not in caplog.text
    assert "<redacted>" in caplog.text


def test_warnings_are_per_kind(monkeypatch, caplog):
    """A registration hiccup must not demote a later, different complaint
    (here: a failing WHEP negotiation) to a debug line — but each kind
    still warns only once."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_add_with = TimeoutError()
    state.fail_whep_with = TimeoutError()
    restreamer = make_restreamer(hass)
    with caplog.at_level(logging.WARNING, logger="restream"):
        run(restreamer.stream_url("cam1"))  # warns: could not register
        run(restreamer.whep_answer("cam1", "v=0 offer"))  # must still warn
        run(restreamer.whep_answer("cam1", "v=0 offer"))  # repeat: debug
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_snapshot_grabs_a_frame_from_the_producer(monkeypatch):
    """Stills come from go2rtc's frame handler on _src — registered first,
    so a freshly restarted go2rtc can serve the very first thumbnail."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    frame = run(make_restreamer(hass).snapshot("cam1"))
    assert frame == b"\xff\xd8fake-jpeg"
    assert ("frame.jpeg", SRC) in state.calls
    assert adds(state) == BOTH  # registered before the grab


def test_snapshot_retries_the_frame_handler_coin_flip_when_hot(monkeypatch):
    """One 500 does not lose the still: extraction takes ~5.4 s against
    the handler's ~5 s patience with this camera's ~4 s GOP, so single
    attempts fail about half the time (the intermittent stills — and the
    stale superdash card — of 2026-08-11). The retry starts mid-GOP, and
    against a running producer it costs nothing."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(ACTIVE_SRC)  # producer hot: retry is free
    state.fail_snapshot_times = 1
    assert run(make_restreamer(hass).snapshot("cam1")) == b"\xff\xd8fake-jpeg"
    assert len([c for c in state.calls if c[0] == "frame.jpeg"]) == 2


def test_snapshot_never_retries_a_cold_producer(monkeypatch):
    """On a cold producer every frame attempt opens a Tuya session, and
    retrying stills was one of the churn sources that exhausted the
    camera's 3-5 slot pool (field, 2026-08-11 10:59). Cold grabs get one
    attempt; FrameCache serves the stale frame instead."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_snapshot_with = TimeoutError()  # streams stay in configured form
    assert run(make_restreamer(hass).snapshot("cam1")) is None
    assert len([c for c in state.calls if c[0] == "frame.jpeg"]) == 1


def test_snapshot_failure_returns_none(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(ACTIVE_SRC)
    state.fail_snapshot_with = TimeoutError()
    assert run(make_restreamer(hass).snapshot("cam1")) is None


def test_snapshot_none_when_no_go2rtc(monkeypatch):
    hass = FakeHass()  # hass.data empty
    assert run(make_restreamer(hass).snapshot("cam1")) is None


# -- the warm-up: the chain must be up before PyAV dials --------------------


def test_cold_open_warms_the_chain(monkeypatch):
    """A cold stream_url() arms a temporary preload on _src_aac so go2rtc
    starts ffmpeg -> _src -> camera BEFORE PyAV's 5s open patience begins;
    a cold open without this failed with "Invalid data found when
    processing input" (field, 2026-08-10 23:54)."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert ("preload.enable", AAC) in state.calls
    assert ("preload.enable", SRC) not in state.calls  # only the chain's head


def test_warmup_skipped_when_chain_already_active(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(ACTIVE_SRC)
    state.streams[AAC] = FakeStream(ACTIVE_AAC)
    run(make_restreamer(hass).stream_url("cam1"))
    assert [c for c in state.calls if c[0] == "preload.enable"] == []


def test_warmup_waits_for_the_audio_track(monkeypatch):
    """ffmpeg registers the video track before the AAC one, and a worker
    that attaches in between gets a video-only SDP: "Audio stream not
    found", a silent recording (field, 2026-08-11 10:50 and 15:25). The
    gate must hold the URL until go2rtc's track list carries BOTH kinds."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.kinds_sequence = [["video"], ["video"], ["video", "audio"]]
    assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    polls = [c for c in state.calls if c[0] == "streams.detail"]
    assert len(polls) == 3  # held through the video-only window
    assert polls[-1][2] == ("video", "audio")


def test_warmup_video_only_forever_still_returns_url(monkeypatch, caplog):
    """A camera with audio off must not lose HLS: past the deadline the
    URL goes out anyway — a silent stream beats none."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.kinds_sequence = [["video"]]
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_warmup_never_consults_the_frame_handler(monkeypatch):
    """The frame handler is not a readiness signal. Gating the warm-up on
    frame.jpeg looked airtight — a JPEG proves decodable video end to
    end — but the handler itself is unreliable against this camera
    (~5.4 s extraction vs its ~5 s patience; the intermittent stills
    failures are the same bug), so the gate burned its whole budget
    confirming nothing and a cold record failed again (field, 2026-08-11
    10:34). The warm-up must complete without a single frame call even
    when the handler is hard down."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_snapshot_with = TimeoutError()  # frame handler hard down
    assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert ("preload.enable", AAC) in state.calls  # still armed
    assert [c for c in state.calls if c[0] == "frame.jpeg"] == []


def test_warmup_timeout_still_returns_url(monkeypatch, caplog):
    """The chain not coming up in time must not fail stream_source():
    the URL is stable, the stream worker retries every 10-then-more
    seconds, and the armed preload holds the chain up across those
    retries, so even a slow cold build is caught by a later dial."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.activate_on_preload = False
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_warmup_failure_still_returns_url(monkeypatch, caplog):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_preload_with = TimeoutError()
    with caplog.at_level(logging.WARNING, logger="restream"):
        assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert "warm up" in caplog.text


def test_warmup_release_disables_after_the_window(monkeypatch):
    """The temporary preload winds down once the real consumer holds the
    chain — a mere stream_url() call must never leave a permanent camera
    session behind; that is keep_stream_running's opt-in, not ours."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    # The release must outlive the poll+settle, as it does at real scale.
    monkeypatch.setattr(restream_mod, "_WARMUP_RELEASE", 0.1)

    async def scenario():
        await make_restreamer(hass).stream_url("cam1")
        assert AAC in state.preloads  # armed
        await asyncio.gather(*hass.tasks)

    run(scenario())
    assert ("preload.disable", AAC) in state.calls
    assert AAC not in state.preloads


def test_warmup_release_skips_a_vanished_preload(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    monkeypatch.setattr(restream_mod, "_WARMUP_RELEASE", 0.1)

    async def scenario():
        await make_restreamer(hass).stream_url("cam1")
        state.preloads.clear()  # go2rtc restarted, or someone disabled it
        await asyncio.gather(*hass.tasks)

    run(scenario())
    assert ("preload.disable", AAC) not in state.calls


def test_no_preload_put_over_an_active_chain(monkeypatch):
    """A preload PUT over a RUNNING producer drops and redials its
    consumer (the destructiveness preload.py documents). Active is the
    only state that protects an armed preload from a re-PUT."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[SRC] = FakeStream(ACTIVE_SRC)
    state.streams[AAC] = FakeStream(ACTIVE_AAC)
    state.preloads[AAC] = {}  # armed and doing its job

    async def scenario():
        await make_restreamer(hass).stream_url("cam1")
        await asyncio.gather(*hass.tasks)

    run(scenario())
    assert ("preload.enable", AAC) not in state.calls
    assert AAC in state.preloads  # never released by us


def test_inert_preload_is_reput_when_the_chain_is_idle(monkeypatch):
    """go2rtc's preload dials exactly once, at PUT time: an entry whose
    dial failed (the camera was dark when some open armed it) is inert,
    and skipping "already armed" then starts nothing — every PyAV dial
    bootstrapped the chain from zero and lost the 5s race (field,
    2026-08-12 08:47). Idle producer => re-PUT, the watchdog's rule."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.preloads[AAC] = {}  # listed, but its one dial failed long ago
    assert run(make_restreamer(hass).stream_url("cam1")) == RTSP
    assert ("preload.enable", AAC) in state.calls  # re-PUT dials fresh


def test_concurrent_cold_opens_arm_once_and_finish(monkeypatch):
    """The check-then-enable is serialized (one arming), but no lock is
    held across the polls — concurrent opens must all complete."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.activate_on_preload = False  # keep both callers polling
    restreamer = make_restreamer(hass)

    async def burst():
        return await asyncio.gather(
            restreamer.stream_url("cam1"), restreamer.stream_url("cam1")
        )

    assert run(burst()) == [RTSP, RTSP]
    assert [c for c in state.calls if c[0] == "preload.enable"] == [("preload.enable", AAC)]


def test_setup_pass_does_not_warm(monkeypatch):
    """warm=False registers only: warming at entry setup would open a Tuya
    session at every HA start for nobody."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    assert run(make_restreamer(hass).stream_url("cam1", warm=False)) == RTSP
    assert preload_calls(state) == []
    assert adds(state) == BOTH  # registration still happened


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
