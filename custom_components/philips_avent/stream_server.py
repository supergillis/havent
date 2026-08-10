"""The signaling endpoint go2rtc dials to reach the camera.

`GET /avent/{camera_id}` speaks go2rtc's own websocket source protocol, which is
what the camera entity's `stream_source()` points at. We relay the offer, the
answer and the ICE candidates through Tuya's cloud MQTT broker; the media then
goes straight from the camera to go2rtc over the LAN. Nothing streams through
this process, and nothing here imports Home Assistant.

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
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from aiohttp import WSMsgType, web

try:
    from . import player
    from .sdp import SdpError, describe, rewrite_answer, rewrite_offer
    from .signaling import Session as TuyaSession
    from .signaling import SignalingError, SignalingHub
except ImportError:  # imported outside the package, e.g. by the tests
    import player
    from sdp import SdpError, describe, rewrite_answer, rewrite_offer
    from signaling import Session as TuyaSession
    from signaling import SignalingError, SignalingHub

_LOGGER = logging.getLogger(__name__)

#: Cameras answer in 74-135 ms when they answer at all; a dial still silent
#: after this is a full session pool, and COOLDOWN takes over from here.
ANSWER_TIMEOUT = 6.0
#: After an unanswered offer, leave the camera alone this long. Its session
#: pool holds 3-5 slots and a full pool closes newcomers, so redialling —
#: which go2rtc does eagerly, twice per stream via its ffmpeg second source —
#: only digs the hole deeper. No cloud call, no offer, until this expires.
COOLDOWN = 25.0
#: A redial this soon after a successful answer means a second consumer is
#: attached (go2rtc only redials when it has no producer). One camera, one
#: consumer: two go2rtc instances will replace — and disconnect — each other.
RECENT_ANSWER = 15.0
#: How long a session keeps listening after the answer. Long enough to hear a
#: stream that dies at birth, short enough that an abandoned one costs nothing.
LINGER = 120.0
RESOLUTION_DELAY = 1.5

AUTH_FAILURES = ("SID_INVALID", "USER_SESSION_LOSS", "USER_SESSION_INVALID")


@dataclass
class CameraSource:
    """One camera, and the account connection it is reached through."""

    camera_id: str
    name: str
    hub: SignalingHub
    token: str
    talkback: bool = False
    on_auth_failed: Callable[[], None] | None = None
    #: Fired after the camera answers a negotiation. That is the earliest
    #: moment the consumer that dialled us (go2rtc) is guaranteed to know
    #: this stream by name, which is what the keep_stream_running glue needs
    #: before it can arm go2rtc's preload. A plain callback so this module
    #: stays HA-free: the Home Assistant side passes it in (see preload.py).
    on_answered: Callable[[], None] | None = None
    last_error: str | None = None
    cooldown_until: float = 0.0


class Stream:
    """One negotiation and the linger that follows it."""

    def __init__(self, server: StreamServer, camera_id: str, session: TuyaSession):
        self.server = server
        self.camera_id = camera_id
        self.session = session
        self.released = False
        self.answered_at: float | None = None
        self._timers: list[asyncio.TimerHandle] = []

    def after(self, delay: float, action: Callable[[], None]) -> None:
        self._timers.append(asyncio.get_running_loop().call_later(delay, action))

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


class StreamServer:
    """One loopback aiohttp site, shared by every config entry using this backend."""

    def __init__(self, port: int, *, bind: str = "127.0.0.1") -> None:
        self.port = port
        self.bind = bind
        self._cameras: dict[str, CameraSource] = {}
        self._streams: dict[str, Stream] = {}
        self._runner: web.AppRunner | None = None

    # -- registry ----------------------------------------------------------

    def add_camera(self, source: CameraSource) -> None:
        self._cameras[source.camera_id] = source

    def remove_camera(self, camera_id: str) -> None:
        self._cameras.pop(camera_id, None)
        if (stream := self._streams.get(camera_id)) is not None:
            stream.release("camera unregistered")

    @property
    def cameras(self) -> dict[str, CameraSource]:
        return self._cameras

    def forget(self, stream: Stream) -> None:
        if self._streams.get(stream.camera_id) is stream:
            del self._streams[stream.camera_id]

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/avent/{camera_id}", self._handle_offer)
        app.router.add_get("/player/{camera_id}", self._handle_player)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.bind, self.port).start()
        _LOGGER.info("Signaling server listening on %s:%d", self.bind, self.port)

    async def stop(self) -> None:
        for stream in list(self._streams.values()):
            # Silent on purpose: reloading a config entry must not black out
            # a stream someone is watching. The linger state is ours alone.
            stream.release("server stopping")
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- negotiation -------------------------------------------------------

    def _authorize(self, request: web.Request) -> CameraSource:
        source = self._cameras.get(request.match_info["camera_id"])
        if source is None or not secrets.compare_digest(request.query.get("t", ""), source.token):
            raise web.HTTPForbidden(text="unknown camera or bad token")
        return source

    async def _handle_player(self, request: web.Request) -> web.Response:
        """The LAN player page (player.py): same token gate as the ws path.

        Reachable off-host only when the `lan_player` option rebound the
        server to the LAN; on the default loopback bind this route exists
        but serves nobody the ws endpoint could not already serve.
        """
        source = self._authorize(request)
        return web.Response(text=player.render(source.name), content_type="text/html")

    async def _negotiate(
        self, source: CameraSource, offer: str, on_candidate: Callable[[str], None]
    ) -> tuple[Stream, str]:
        """Run one offer/answer exchange; return the stream and the answer."""
        loop = asyncio.get_running_loop()
        if (remaining := source.cooldown_until - loop.time()) > 0:
            raise SignalingError(
                f"{source.name} did not answer a recent offer; not dialling again "
                f"for another {remaining:.0f}s so its session pool can drain"
            )

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

        session = await source.hub.open_session(source.camera_id)
        stream = Stream(self, source.camera_id, session)
        self._streams[source.camera_id] = stream

        answer: asyncio.Future[str] = loop.create_future()

        def got_answer(sdp: str) -> None:
            if not answer.done():
                answer.set_result(sdp)

        session.on_answer = got_answer
        session.on_candidate = on_candidate
        session.on_disconnect = lambda: stream.release("camera disconnected")

        try:
            camera_offer = rewrite_offer(offer, talkback=source.talkback)
            _LOGGER.debug("Offering %s to %s", describe(camera_offer), source.name)
            session.send_offer(camera_offer)

            async with asyncio.timeout(ANSWER_TIMEOUT):
                camera_answer = await answer
            # Inside the try on purpose: an answer we cannot rewrite is a
            # failed handshake too, and must free its slot like one.
            consumer_answer = rewrite_answer(camera_answer, offer)
        except TimeoutError as err:
            source.cooldown_until = loop.time() + COOLDOWN
            stream.release("no answer", disconnect=True)
            raise SignalingError(
                f"{source.name} did not answer within {ANSWER_TIMEOUT:.0f}s "
                "(offline, or its 3-5 slot session pool is full)"
            ) from err
        except Exception:
            stream.release("handshake failed", disconnect=True)
            raise

        source.cooldown_until = 0.0
        stream.answered_at = loop.time()
        _LOGGER.debug("%s answered %s", source.name, describe(camera_answer))

        stream.after(RESOLUTION_DELAY, session.send_resolution)
        stream.after(LINGER, lambda: stream.release("linger expired"))
        if source.on_answered is not None:
            # Every answer, not just the first: a preload can stop doing
            # its job behind our back (go2rtc restarted, or HA's provider
            # disabled it), and the next answer is when we find out. The
            # callback must be idempotent — preload.py checks whether the
            # preload is still armed before re-arming, because go2rtc's
            # preload PUT tears down and redials the producer, i.e. the
            # very session that just answered.
            source.on_answered()
        return stream, consumer_answer

    def _record_failure(self, source: CameraSource, err: Exception) -> None:
        source.last_error = str(err)
        if any(code in str(err) for code in AUTH_FAILURES) and source.on_auth_failed:
            source.on_auth_failed()

    async def _handle_offer(self, request: web.Request) -> web.WebSocketResponse:
        """go2rtc's source protocol: offer in, answer next, then candidates.

        The answer has to be the very next message on the socket, so candidates
        arriving before it are held back.
        """
        source = self._authorize(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        stream: Stream | None = None
        early: list[str] = []
        answered = False
        sends: set[asyncio.Task] = set()

        async def send_candidate(candidate: str) -> None:
            # Suppressed: the socket can close between relay's check and the send.
            with contextlib.suppress(ConnectionResetError):
                await ws.send_json({"type": "webrtc/candidate", "value": candidate})

        def relay(candidate: str) -> None:
            if not answered:
                early.append(candidate)
                return
            if ws.closed:
                # Expected: go2rtc drops the socket once ICE is up, and the
                # camera keeps trickling.
                return
            # Keep a reference until done, or the task can be GC'd mid-send.
            task = asyncio.get_running_loop().create_task(send_candidate(candidate))
            sends.add(task)
            task.add_done_callback(sends.discard)

        try:
            async for message in ws:
                if message.type is not WSMsgType.TEXT:
                    continue
                frame = message.json()
                if frame.get("type") == "webrtc/offer":
                    stream, answer = await self._negotiate(source, frame["value"], relay)
                    await ws.send_json({"type": "webrtc/answer", "value": answer})
                    answered = True
                    for candidate in early:
                        await ws.send_json({"type": "webrtc/candidate", "value": candidate})
                    early.clear()
                    source.last_error = None
                elif frame.get("type") == "webrtc/candidate" and stream is not None:
                    if value := frame.get("value"):
                        stream.session.send_candidate(value)
        except (SignalingError, SdpError) as err:
            self._record_failure(source, err)
            _LOGGER.error("Stream setup failed for %s: %s", source.name, err)
            await self._report(ws, err)
        except Exception as err:
            self._record_failure(source, err)
            _LOGGER.exception("Stream setup failed for %s", source.name)
            await self._report(ws, err)

        # go2rtc closes this socket as soon as ICE connects. The stream stays.
        await ws.close()
        return ws

    @staticmethod
    async def _report(ws: web.WebSocketResponse, err: Exception) -> None:
        """Hand the reason to go2rtc, which puts it in its log and its API."""
        if not ws.closed:
            await ws.send_json({"type": "error", "value": str(err)})
