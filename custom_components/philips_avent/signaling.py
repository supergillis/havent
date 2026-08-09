"""Tuya WebRTC signaling: the cloud handshake and the MQTT offer/answer exchange.

This is everything the Go bridge did before media started
(`pkg/tuya/{mobile_api,mqtt,mqttCamera}.go`). No media passes through it: a
WebRTC stack elsewhere — go2rtc, or a browser — owns the SRTP side, and this
module only relays SDP and ICE candidates through Tuya's cloud MQTT broker.

One broker connection per account, shared by every camera and every concurrent
session, exactly like the Go bridge's `MQTTManager`. That is not an
optimisation: the client id Tuya expects is derived from the account and the
phone device id with nothing per-session in it, so a second connection would be
kicked off the broker by the first.

Nothing here imports Home Assistant.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

MQTT_PORT = 8883
MQTT_KEEPALIVE = 60
CONNECT_TIMEOUT = 15.0

# Frame protocol numbers, taken from the app's traffic.
PROTOCOL_SESSION = 302  # offer, answer, candidate, disconnect
PROTOCOL_CONTROL = 312  # resolution, speaker

CODEC_HEVC = 4


class SignalingError(Exception):
    """The cloud or the camera refused to set up a session."""


@dataclass(frozen=True)
class Credentials:
    """What one account contributes to a signaling session."""

    sid: str
    ecode: str
    partner: str
    device_id: str


class SignalingHub:
    """One account's broker connection, shared by all of its cameras."""

    def __init__(self, api: Any, credentials: Credentials) -> None:
        self.api = api
        self.credentials = credentials
        self.uid = ""
        self._host = ""
        self._client: mqtt.Client | None = None
        self._connected: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    # -- connection --------------------------------------------------------

    async def _connect(self) -> None:
        """Connect if not already connected. Safe to call per session."""
        async with self._lock:
            if self._client is not None:
                return

            user = await self.api.get_user_info()
            self.uid = user["id"]
            self._host = user["domain"]["mobileMqttsUrl"]

            creds = self.credentials
            username = self.api.derive_mqtt_username(creds.sid, creds.ecode, creds.partner)
            password = self.api.derive_mqtt_password(creds.ecode)
            client_id = self.api.derive_mqtt_client_id(self.uid, creds.device_id)

            loop = self._loop = asyncio.get_running_loop()
            self._connected = asyncio.Event()
            # tls_set() reads the system CA bundle, so it cannot run on the loop.
            client = await loop.run_in_executor(None, _build_client, client_id, username, password)
            topic = f"/av/u/{self.uid}"
            client.on_connect = lambda c, u, f, rc, props=None: self._on_connect(c, rc, topic)
            client.on_message = lambda c, u, msg: loop.call_soon_threadsafe(
                self._dispatch, msg.payload
            )
            self._client = client

            client.connect_async(self._host, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            client.loop_start()
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await self._connected.wait()
            except TimeoutError as err:
                await self._drop()
                raise SignalingError(
                    f"no Tuya MQTT connection to {self._host} within {CONNECT_TIMEOUT:.0f}s"
                ) from err
            _LOGGER.debug("Signaling connected as %s, listening on %s", client_id, topic)

    def _on_connect(self, client: mqtt.Client, reason: Any, topic: str) -> None:
        """paho thread."""
        if getattr(reason, "is_failure", False):
            # Wrong or expired credentials: paho would otherwise retry them
            # forever. Drop the connection so the next stream rebuilds it from
            # a fresh session id.
            _LOGGER.warning("Tuya MQTT refused the connection: %s", reason)
            client.disconnect()
            return
        client.subscribe(topic, qos=1)
        if self._connected is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.set)

    async def _drop(self) -> None:
        client, self._client = self._client, None
        self._connected = None
        if client is None:
            return
        await asyncio.get_running_loop().run_in_executor(None, _stop_client, client)

    async def close(self) -> None:
        """Release the broker connection. Says nothing to any camera."""
        self._sessions.clear()
        async with self._lock:
            await self._drop()

    # -- sessions ----------------------------------------------------------

    async def open_session(
        self, camera_id: str, *, resolution: str = "hd", talkback: bool = False
    ) -> Session:
        """Cloud handshake for one camera, then a live session on the broker."""
        session = Session(self, camera_id, resolution=resolution, talkback=talkback)
        await session.prepare()
        await self._connect()
        self._sessions[session.session_id] = session
        return session

    def _dispatch(self, payload: bytes) -> None:
        """Event loop, via call_soon_threadsafe."""
        try:
            frame = json.loads(payload)
            header = frame["data"]["header"]
            body = frame["data"].get("msg") or {}
        except (ValueError, KeyError, TypeError):
            _LOGGER.debug("Ignoring unparseable signaling frame: %r", payload[:120])
            return

        # The topic is per account: every camera and every session lands here.
        session = self._sessions.get(header.get("sessionid", ""))
        if session is not None:
            session.handle(header.get("type", ""), body)

    def publish(self, topic: str, frame: dict[str, Any]) -> None:
        if (client := self._client) is not None:
            client.publish(topic, json.dumps(frame), qos=1)

    def release(self, session: Session) -> None:
        self._sessions.pop(session.session_id, None)


class Session:
    """One camera's offer/answer exchange.

    Closing a session stops us listening. Whether the camera is *told* is
    the caller's choice: `send_disconnect` is session-scoped and frees our
    slot in the camera's small session pool, so the stream server sends it
    when it abandons a session — and stays silent whenever media may still
    be flowing.
    """

    def __init__(
        self,
        hub: SignalingHub,
        camera_id: str,
        *,
        resolution: str = "hd",
        talkback: bool = False,
    ) -> None:
        self.hub = hub
        self.camera_id = camera_id
        self.resolution = resolution
        self.talkback = talkback
        self.session_id = secrets.token_hex(16)
        self.ice_servers: list[dict[str, Any]] = []

        self._auth = ""
        self._moto_id = ""
        self._topic = ""
        self._stream_type = 1

        # Called on the event loop.
        self.on_answer: Callable[[str], None] | None = None
        self.on_candidate: Callable[[str], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

    async def prepare(self) -> None:
        """Per-camera cloud calls: the session's auth token, moto id and ICE servers."""
        api = self.hub.api

        # Advisory: the app sends both, and the config request can succeed
        # without them.
        for warm_up in (api.p2p_prelink, api.rtc_session_init):
            try:
                await warm_up(self.camera_id)
            except Exception as err:  # noqa: BLE001 - advisory call, any failure is survivable
                _LOGGER.debug("%s failed for %s: %s", warm_up.__name__, self.camera_id, err)

        config = await api.get_rtc_config(self.camera_id)
        self._moto_id = config.get("motoId") or ""
        self._auth = config.get("auth") or ""
        self.ice_servers = config.get("p2pConfig", {}).get("ices", [])
        if not self._moto_id:
            raise SignalingError("camera returned no signaling id; it may be offline")
        self._topic = f"/av/moto/{self._moto_id}/u/{self.camera_id}"

        skill = _parse_skill(config.get("skill"))
        self._stream_type = _stream_type(skill, self.resolution)
        if _is_hevc(skill, self._stream_type):
            raise SignalingError(
                "this camera streams HEVC, which the built-in backend cannot relay"
            )

    # -- inbound -----------------------------------------------------------

    def handle(self, kind: str, body: dict[str, Any]) -> None:
        if kind == "answer" and self.on_answer:
            self.on_answer(body.get("sdp", ""))
        elif kind == "candidate" and self.on_candidate:
            candidate = (body.get("candidate") or "").strip().removeprefix("a=").rstrip("\r")
            if candidate:
                self.on_candidate(candidate)
        elif kind == "disconnect" and self.on_disconnect:
            self.on_disconnect()

    # -- outbound ----------------------------------------------------------

    def _publish(self, kind: str, protocol: int, body: dict[str, Any], session_id: str) -> None:
        self.hub.publish(self._topic, {
            "protocol": protocol,
            "pv": "2.2",
            "t": int(time.time() * 1000),
            "data": {
                "header": {
                    "type": kind,
                    "from": self.hub.uid,
                    "to": self.camera_id,
                    "sub_dev_id": "",
                    "sessionid": session_id,
                    "moto_id": self._moto_id,
                    "tid": "",
                    "seq": 0,
                    "rtx": 0,
                },
                "msg": body,
            },
        })

    def send_offer(self, sdp: str) -> None:
        self._publish("offer", PROTOCOL_SESSION, {
            "mode": "webrtc",
            "sdp": sdp,
            "stream_type": self._stream_type,
            "auth": self._auth,
            "token": self.ice_servers,
            "replay": {"is_replay": 0},
            "datachannel_enable": False,
        }, self.session_id)

    def send_candidate(self, candidate: str) -> None:
        self._publish("candidate", PROTOCOL_SESSION, {
            "mode": "webrtc",
            "candidate": candidate if candidate.startswith("a=") else f"a={candidate}",
        }, self.session_id)

    def send_resolution(self, value: int = 0) -> None:
        """0 = HD, 1 = SD. Sent once the peer connection should be up."""
        self._publish("resolution", PROTOCOL_CONTROL, {
            "mode": "webrtc", "cmdValue": value,
        }, self.session_id)

    def send_disconnect(self) -> None:
        """Tell the camera this session is over, freeing its pool slot.

        Deliberately takes no session id: the frame always names our own
        session, so disconnecting someone else's is impossible to express.
        Matches the Go bridge (`bridge.go` `Stop()`) and go2rtc's own
        `pkg/tuya`, which both send it for their own session at teardown.
        """
        self._publish("disconnect", PROTOCOL_SESSION, {"mode": "webrtc"}, self.session_id)

    def close(self) -> None:
        self.hub.release(self)


def _build_client(client_id: str, username: str, password: str) -> mqtt.Client:
    """Blocking: tls_set() loads the system CA bundle from disk."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.username_pw_set(username, password)
    client.tls_set()
    return client


def _stop_client(client: mqtt.Client) -> None:
    """Blocking: loop_stop() joins the network thread."""
    client.disconnect()
    client.loop_stop()


def _parse_skill(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        _LOGGER.debug("Camera returned an unparseable skill: %r", raw)
        return {}


def _stream_type(skill: dict[str, Any], resolution: str) -> int:
    """Which of the camera's streams to ask for.

    Carried over from the Go bridge verbatim, including the default: this field
    is the skill's own `streamType` (2 for the main stream, 4 for the sub
    stream on the models seen so far), not an index.
    """
    videos = skill.get("videos") or []
    if not videos:
        return 1
    by_pixels = sorted(videos, key=lambda video: video.get("width", 0) * video.get("height", 0))
    chosen = by_pixels[-1] if resolution == "hd" else by_pixels[0]
    return chosen.get("streamType", 1)


def _is_hevc(skill: dict[str, Any], stream_type: int) -> bool:
    return any(
        video.get("codecType") == CODEC_HEVC
        for video in skill.get("videos") or []
        if video.get("streamType") == stream_type
    )
