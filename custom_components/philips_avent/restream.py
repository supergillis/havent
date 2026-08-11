"""Registering the builtin backend's producer stream in go2rtc.

The camera's `stream_source()` used to hand out the signaling `webrtc:ws`
URL directly; HA's stream component (HLS, `camera.record`, casting) cannot
open that scheme, so those features were lost. Instead we register TWO
go2rtc streams: the producer `philips_avent_<id>_src`, whose ONLY source
is the ws URL, and `philips_avent_<id>_src_aac`, whose only source is an
`ffmpeg:` pull of the producer's RTSP with video copied and audio
transcoded to AAC (HA's stream component drops any audio that is not
AAC/MP3, and the camera sends PCMU). `stream_source()` hands out the RTSP
URL of `_src_aac`; live view (WHEP) and frame grabs hit `_src` directly.
Everything still fans out from one Tuya session — RTSP consumers of
`_src` share the running producer. The split is load-bearing: the ws
endpoint serves exactly one consumer, and a second source on the same
stream gave go2rtc something to start, EOF and redial against the very
session that was already running — the 2026-08-10 session storm. Live
view and stills go through here too: the builtin camera is a
native-WebRTC entity (overriding the offer handler means HA never
attaches its go2rtc provider), so WHEP negotiation and frame grabs are
ours to make, against the producer.

Where go2rtc's RTSP listens must be probed, not assumed: HA's managed
instance pins it to 127.0.0.1:18554 in its config template, user-run ones
default to :8554 on any host. The probe is `GET /api` (bare `/api` is on
the managed instance's allow_paths whitelist) and every request rides
`hass.data["go2rtc"]`'s own session — the managed API is a unix socket
with per-boot local_auth, unreachable any other way.

The recording chain must be WARM before PyAV dials it. HA's stream
component opens with a hardcoded 5 s socket timeout and no hook to raise
it, while a cold `_src_aac` needs ffmpeg, a Tuya session and a keyframe
first (~5-8 s measured) — so `stream_url()` arms a temporary preload on
`_src_aac`, waits for an active producer, and releases the preload 90 s
later (see _warm_up; the 2026-08-10 23:54 "Invalid data" failures).

The one rule everything here serves: never break live view to gain HLS.
HA's `Camera.async_create_stream` calls `stream_source()` at most once per
entity lifetime and freezes the result into its cached Stream. So only a
permanent, config-shaped verdict (no go2rtc, RTSP disabled, remote
go2rtc with loopback RTSP) may return the ws fallback; transient trouble
returns the stable RTSP URL anyway — the stream worker retries its
source, and registration catches up via WHEP live-view opens, frame
grabs and the setup pass. `PUT /api/streams` silently replaces a
live stream without stopping its producers, so registration is
check-then-PUT under a lock, exactly like preload.py's arming.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

try:
    from .const import go2rtc_aac_name, go2rtc_producer_name
    from .preload import _GO2RTC_DATA, describe_error, go2rtc_rest_client
except ImportError:  # imported outside the package, e.g. by the tests
    from const import go2rtc_aac_name, go2rtc_producer_name
    from preload import _GO2RTC_DATA, describe_error, go2rtc_rest_client

try:
    from go2rtc_client import WebRTCSdpOffer
except ImportError:  # go2rtc integration (and its requirement) not installed
    WebRTCSdpOffer = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

#: The probe must not hang stream_source(): HA's default session has no
#: total timeout, and a stuck GET here would stall every stream open.
_PROBE_TIMEOUT = 5.0

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
_ALL_INTERFACES = ("", "0.0.0.0", "::", "[::]")

#: Where HA's config template pins the managed instance's RTSP listener.
#: Used only as the PROVISIONAL endpoint when the /api probe fails
#: transiently on a loopback API host — never cached, re-probed next call,
#: version-coupled to HA's template and the log says so. The point is that
#: stream_source()'s result is frozen into HA's cached Stream, so a
#: transient failure must still yield the stable RTSP URL (see spec).
MANAGED_RTSP = ("127.0.0.1", 18554)

#: How long stream_url() may wait for the recording chain to come up before
#: handing out the URL anyway. Must clear HA's hard ceiling on
#: stream_source() — camera.const.CAMERA_STREAM_SOURCE_TIMEOUT, 10 s — with
#: room left for the probe and registration; PyAV's own 5 s socket timeout
#: only starts ticking after this, so the chain gets ~13 s in total against
#: a measured ~5-8 s cold latency.
_WARMUP_TIMEOUT = 6.0
_WARMUP_POLL = 0.5
#: Grace between the producer turning active and handing out the URL: a
#: producer that JUST connected may not have tracks ready for the
#: consumer PyAV is about to be. Sized so timeout + settle + the cached
#: probe stay under HA's 10 s CAMERA_STREAM_SOURCE_TIMEOUT.
_WARMUP_SETTLE = 2.0
#: How long the warm-up preload holds the chain once the URL went out. By
#: then the real consumer (PyAV) is attached and keeps the chain alive on
#: its own; if none ever came, the chain winds down instead of streaming
#: the camera for nobody.
_WARMUP_RELEASE = 90.0


#: Source schemes as we configure them. `GET /api/streams` reports an IDLE
#: producer as its configured source string, but an ACTIVE producer
#: delegates serialization to its connection (go2rtc v1.9.14
#: internal/streams/producer.go MarshalJSON: `if conn := p.conn; conn !=
#: nil { return json.Marshal(conn) }`), whose url is the resolved form —
#: an ffmpeg source comes back as the expanded `exec:ffmpeg ...` command
#: line. A reported url outside these schemes therefore means "running",
#: not "wrong".
_CONFIGURED_SCHEMES = ("webrtc:", "ffmpeg:")


def needs_registration(key_source: str, reported: list[str] | None) -> bool:
    """Whether the check half of check-then-PUT should PUT.

    `key_source` is the stream's identity source (the ws URL for `_src`);
    `reported` is what go2rtc's API returned for the stream's producers, or
    None when the stream does not exist. The table, biased hard toward
    skip — a wrong re-PUT replaces the map entry without stopping the old
    producers (go2rtc streams.New), whose immortal retry workers then fight
    the new ones over the camera's one-consumer ws endpoint (the ~5s
    Tuya-session storm of 2026-08-10); a wrong skip merely leaves a stale
    source that self-corrects on the next entry reload:

    - stream absent                          -> PUT (cold registration)
    - key_source among the reported urls     -> skip (ours, idle match)
    - any reported url not in a configured
      scheme (e.g. `exec:...`)               -> skip (running; urls are
                                                resolved, compare says
                                                nothing — even a genuine
                                                token change waits)
    - all configured-form, key_source absent -> PUT (genuinely stale, idle)
    """
    if reported is None:
        return True
    if key_source in reported:
        return False
    # PUT only when every reported url is legible (idle, configured form).
    return all(url.startswith(_CONFIGURED_SCHEMES) for url in reported)


def _looks_active(stream) -> bool:
    """Whether go2rtc reports this stream as running.

    Same signal needs_registration reads defensively: an active producer
    serializes in resolved form, outside the schemes we configure. Absent
    stream or all-configured-form producers means idle.
    """
    if stream is None:
        return False
    return any(not p.url.startswith(_CONFIGURED_SCHEMES) for p in stream.producers)


def parse_rtsp_endpoint(listen: str | None, api_host: str) -> tuple[str, int] | None:
    """(host, port) HA's stream worker can dial, or None to fall back."""
    if not listen:
        return None
    host, _, port_text = listen.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return None
    api_local = api_host in _LOOPBACK_HOSTS
    if host in _ALL_INTERFACES:
        return ("127.0.0.1", port) if api_local else (api_host, port)
    if host in _LOOPBACK_HOSTS:
        return ("127.0.0.1", port) if api_local else None
    return (host, port)


class Restreamer:
    """Registers each camera's producer in go2rtc and hands out its RTSP URL."""

    def __init__(self, hass: HomeAssistant, ws_url: Callable[[str], str]) -> None:
        self._hass = hass
        self._ws_url = ws_url
        self._endpoint: tuple[str, int] | None = None  # cached on success only
        self._no_rtsp = False  # the permanent this-config-has-no-RTSP verdict
        self._lock = asyncio.Lock()  # serializes the check-then-PUT
        self._warm_lock = asyncio.Lock()  # serializes the check-then-preload; never held across polls
        self._warned: set[str] = set()  # complaint kinds already warned about

    async def stream_url(self, cam_id: str, *, warm: bool = True) -> str:
        """The producer's RTSP URL, or the webrtc: fallback — but the
        fallback ONLY for permanent, config-shaped verdicts (no go2rtc,
        RTSP disabled, remote-loopback). HA's stream component calls
        stream_source() at most once per entity lifetime and freezes the
        result, so a transient failure must still return the stable RTSP
        URL: the stream worker retries its source, and re-registration
        arrives via WHEP live-view opens, frame grabs and the setup pass.

        `warm=True` (the stream_source() path) also starts the recording
        chain before the URL goes out — see _warm_up. The setup pass
        registers with warm=False: arming a camera session at every HA
        start, for nobody, is exactly what keep_stream_running exists to
        opt into."""
        endpoint = await self._rtsp_endpoint()  # None only for permanent verdicts
        if endpoint is None:
            return self._ws_url(cam_id)
        await self._ensure_registered(cam_id)  # best effort; the URL is stable either way
        if warm:
            await self._warm_up(cam_id)  # best effort; bounded
        host, port = endpoint
        # No codec-filter query: `_src_aac` carries exactly H.264 + AAC by
        # construction, so PyAV cannot pick up a codec HA's stream drops.
        return f"rtsp://{host}:{port}/{go2rtc_aac_name(cam_id)}"

    def sources(self, cam_id: str) -> list[str]:
        """The producer's ONLY source: the signaling ws endpoint. One
        source by design — the endpoint serves exactly one consumer, and a
        second source here gave go2rtc something to start, EOF and redial
        against the session already running (the 2026-08-10 storm's fuel).
        The AAC transcode lives on its own stream: aac_sources()."""
        return [self._ws_url(cam_id)]

    def aac_sources(self, cam_id: str) -> list[str]:
        """The recording stream's source: ffmpeg pulling `_src`'s RTSP,
        video copied, PCMU transcoded to AAC — HA's stream component drops
        any audio that is not AAC/MP3, and silent recordings on a baby
        monitor are worse than none. `#video=copy` is load-bearing:
        `#audio=aac` alone renders `-vn`, an audio-only stream (go2rtc
        internal/ffmpeg). This dials go2rtc's own RTSP of `_src`, never
        the camera: RTSP consumers fan out from the running producer."""
        return [f"ffmpeg:{go2rtc_producer_name(cam_id)}#video=copy#audio=aac"]

    def _complain_once(self, kind: str, message: str) -> None:
        """One warning per complaint KIND; repeats drop to debug.

        Keyed, not a single flag: a transient registration hiccup at setup
        must not demote a later, permanent "HLS unavailable" verdict to a
        debug line nobody sees.
        """
        if kind in self._warned:
            _LOGGER.debug("%s", message)
            return
        self._warned.add(kind)
        _LOGGER.warning("%s", message)

    async def _rtsp_endpoint(self) -> tuple[str, int] | None:
        """Where go2rtc's RTSP listens, or None to fall back to the ws URL.

        Success and the no-RTSP verdict are config-shaped and cached for
        the entry's lifetime; a failed request is transient — assume the
        managed instance's pinned endpoint when the API host is loopback,
        cache nothing, and re-probe on the next call.
        """
        if self._no_rtsp:
            return None
        if self._endpoint is not None:
            return self._endpoint
        config = self._hass.data.get(_GO2RTC_DATA)
        url = getattr(config, "url", None)
        session = getattr(config, "session", None)
        if not url or session is None:
            self._complain_once(
                "no-go2rtc",
                "Home Assistant's go2rtc is not available; HLS, recording and "
                "casting are off until it is (live view may still work)",
            )
            return None  # rechecked next call: go2rtc may just not be up yet
        api_host = urlsplit(url).hostname or ""
        status = None
        info = None
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT):
                async with session.get(url.rstrip("/") + "/api") as resp:
                    status = resp.status
                    if status == 200:
                        info = await resp.json()
        except Exception as err:  # noqa: BLE001 - a failed probe must never break stream_source
            return self._probe_trouble(describe_error(err), api_host)
        if info is None:
            if status is not None and 400 <= status < 500:
                # Config-shaped: this go2rtc will keep refusing the probe
                # (auth we do not have, a path ACL), so treat it like no
                # RTSP rather than guessing endpoints forever.
                self._no_rtsp = True
                self._complain_once(
                    "no-rtsp",
                    f"go2rtc's API refused the /api probe (HTTP {status}); HLS, "
                    "recording and casting are unavailable (live view is unaffected)",
                )
                return None
            return self._probe_trouble(f"HTTP {status}", api_host)
        endpoint = parse_rtsp_endpoint((info.get("rtsp") or {}).get("listen"), api_host)
        if endpoint is None:
            self._no_rtsp = True
            self._complain_once(
                "no-rtsp",
                "go2rtc has no RTSP endpoint Home Assistant could reach; HLS, "
                "recording and casting are unavailable (live view is unaffected)",
            )
            return None
        self._endpoint = endpoint
        return endpoint

    def _probe_trouble(self, reason: str, api_host: str) -> tuple[str, int] | None:
        """A transient probe failure: cache nothing, re-probe next call."""
        if api_host in _LOOPBACK_HOSTS:
            _LOGGER.debug(
                "go2rtc /api probe failed (%s); assuming the managed "
                "instance's RTSP at %s:%d until a probe succeeds",
                reason, *MANAGED_RTSP,
            )
            return MANAGED_RTSP
        self._complain_once("probe", f"go2rtc /api probe failed ({reason})")
        return None

    async def whep_answer(self, cam_id: str, offer_sdp: str) -> str | None:
        """Single-hop live view: negotiate the producer stream over WHEP.

        A provider-style path would consume the AAC-filtered RTSP and
        transcode PCMU→AAC→opus for every viewer; WHEP against `_src` keeps
        the native audio and one hop, as frigate-hass-integration does.
        Registration is ensured first so a freshly restarted go2rtc can
        answer. Returns None — never raises — when go2rtc or the client
        model is missing or the call fails; the camera reports that to the
        frontend, because a native-WebRTC entity has no provider to fall
        back on.
        """
        if (client := go2rtc_rest_client(self._hass)) is None or WebRTCSdpOffer is None:
            return None
        await self._ensure_registered(cam_id)
        name = go2rtc_producer_name(cam_id)
        try:
            answer = await client.webrtc.forward_whep_sdp_offer(name, WebRTCSdpOffer(offer_sdp))
        except Exception as err:  # noqa: BLE001 - the camera turns None into a frontend error
            self._complain_once(
                "whep",
                f"WHEP against {name} failed ({describe_error(err)}); live view "
                "is down until go2rtc recovers",
            )
            return None
        return answer.sdp

    async def snapshot(self, cam_id: str) -> bytes | None:
        """One JPEG via go2rtc's frame handler, on the producer stream.

        Shares a hot `_src` (someone watching, or keep_stream_running) for
        free; a cold grab dials the producer and stops it again — one Tuya
        session per cache miss, the same cost the old provider frame path
        had. Never raises: a still is decoration, not worth breaking.

        One retry, because the frame handler is a coin flip against this
        camera: extraction takes ~5.4 s (a ~4 s GOP at 1080p plus connect
        overhead) against the handler's own ~5 s patience, so a single
        attempt 500s roughly half the time — the intermittent stills of
        2026-08-11. The retry starts a fresh wait mid-GOP and usually
        lands; what still fails is absorbed by FrameCache serving the
        stale frame for a TTL. Exactly one retry: on a cold producer each
        attempt is a Tuya session, and a still is not worth three.
        """
        if (client := go2rtc_rest_client(self._hass)) is None:
            return None
        await self._ensure_registered(cam_id)
        name = go2rtc_producer_name(cam_id)
        for attempt in (1, 2):
            try:
                return await client.get_jpeg_snapshot(name)
            except Exception as err:  # noqa: BLE001 - a failed still must never raise into the cache
                if attempt == 1:
                    continue
                self._complain_once(
                    "snapshot",
                    f"go2rtc frame grab for {name} failed twice ({describe_error(err)})",
                )
        return None

    async def _ensure_registered(self, cam_id: str) -> None:
        """Check-then-PUT under the lock. PUT /api/streams silently replaces
        a live stream without stopping its producers, so a blind PUT here is
        the same Tuya-session feedback loop preload.py refuses — and the
        check itself must tolerate go2rtc reporting ACTIVE producers in
        resolved form (see needs_registration; a naive url compare re-PUT
        every open while the stream ran, which WAS that feedback loop)."""
        if (client := go2rtc_rest_client(self._hass)) is None:
            return
        try:
            async with self._lock:
                streams = await client.streams.list()
                for name, want in (
                    (go2rtc_producer_name(cam_id), self.sources(cam_id)),
                    (go2rtc_aac_name(cam_id), self.aac_sources(cam_id)),
                ):
                    stream = streams.get(name)
                    reported = [p.url for p in stream.producers] if stream else None
                    if needs_registration(want[0], reported):
                        await client.streams.add(name, want)
                        # The name, never the sources: the ws URL embeds the token.
                        _LOGGER.info("Registered go2rtc stream %s", name)
        except Exception as err:  # noqa: BLE001 - registration is best effort; the URL is stable
            self._complain_once(
                "register",
                f"Could not register go2rtc streams for {go2rtc_producer_name(cam_id)} "
                f"({describe_error(err)}); will retry on the next frame grab or live view",
            )

    async def _warm_up(self, cam_id: str) -> None:
        """Start the recording chain before the URL goes out.

        PyAV opens the RTSP URL with a hardcoded 5 s socket timeout
        (stream's _convert_stream_options) and no supported way for a
        camera to raise it — Camera.stream_options is schema-limited to
        transport, wallclock and part-wait. A cold `_src_aac` needs go2rtc
        to launch ffmpeg, ffmpeg to dial `_src`, the ws source to build a
        Tuya session and a first keyframe to arrive — longer than PyAV's
        patience, so a cold `camera.record`/HLS open failed with "Invalid
        data found when processing input" and every 15 s stream-worker
        retry dialled a fresh camera session (field, 2026-08-10 23:54).

        The lever is a TEMPORARY preload on `_src_aac`: an API-side
        consumer — the same lever keep_stream_running uses, on a different
        name — that makes go2rtc start the chain now. We arm it only if
        nobody else has (a preload PUT over an existing one drops and
        redials its consumer) and release our arming _WARMUP_RELEASE
        later, when the real consumer holds the chain — or nobody came
        and it winds down. keep_stream_running's own preload lives on
        `_src` and is never released here.

        Readiness is ACTIVE-PLUS-SETTLE, stamped safe by wallclock. Two
        earlier gates each failed in the field. Polling for the producer
        connection alone let PyAV attach to a just-started chain that
        served collapsed timestamps — dts +1 tick/frame, so segments
        never cut and camera.record hung silently for 54 minutes
        (2026-08-11 00:25); that pathology is now defused where it bites,
        by AventBuiltinCamera's use_wallclock_as_timestamps. Its
        replacement — polling go2rtc's frame handler for a JPEG — proved
        a dead gate: extraction from this camera takes ~5.4 s (a ~4 s GOP
        at 1080p plus connect overhead) against the handler's own ~5 s
        patience, so frame.jpeg 500s more often than not (the same
        unreliability behind the intermittent stills), the gate burned
        its whole budget confirming nothing, and a cold record failed
        with "Invalid data" again (2026-08-11 10:34). So: wait until the
        producer reports active, then hold the URL a settle period
        longer so the fresh producer has tracks ready for the consumer
        PyAV is about to be. The chain already hot skips the settle.

        The locks: the check-then-enable is serialized so two concurrent
        opens arm once, but no lock is held across the polls. Never
        raises; a failed warm-up still hands out the URL — the stream
        worker retries every 10-then-more seconds, and the armed preload
        holds the chain up across those retries, so even a slow cold
        build is caught by a later dial.
        """
        if (client := go2rtc_rest_client(self._hass)) is None:
            return
        name = go2rtc_aac_name(cam_id)
        armed_here = False
        try:
            async with self._warm_lock:
                if name not in await client.preload.list():
                    streams = await client.streams.list()
                    if _looks_active(streams.get(name)):
                        return  # hot chain, serving consumers already
                    await client.preload.enable(name)
                    armed_here = True
            if armed_here:
                self._hass.async_create_background_task(
                    self._release_warmup(name), f"philips_avent warmup release {name}"
                )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _WARMUP_TIMEOUT
            while loop.time() < deadline:
                await asyncio.sleep(_WARMUP_POLL)
                streams = await client.streams.list()
                if _looks_active(streams.get(name)):
                    await asyncio.sleep(_WARMUP_SETTLE)
                    return
            _LOGGER.debug(
                "%s did not come up within %.0fs; handing out the URL anyway",
                name, _WARMUP_TIMEOUT,
            )
        except Exception as err:  # noqa: BLE001 - warm-up is best effort; the URL is stable
            self._complain_once(
                "warmup",
                f"Could not warm up {name} ({describe_error(err)}); the first "
                "recording or HLS open may fail and retry",
            )

    async def _release_warmup(self, name: str) -> None:
        """Give the warm-up preload back once the real consumer has the chain."""
        await asyncio.sleep(_WARMUP_RELEASE)
        if (client := go2rtc_rest_client(self._hass)) is None:
            return
        try:
            if name in await client.preload.list():
                await client.preload.disable(name)
                _LOGGER.debug("Released the warm-up preload on %s", name)
        except Exception as err:  # noqa: BLE001 - best effort; go2rtc restarts clear preloads anyway
            _LOGGER.debug(
                "Could not release the warm-up preload on %s: %s", name, describe_error(err)
            )
