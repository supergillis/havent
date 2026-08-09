"""Camera entity for Philips Avent Baby Monitor."""
from __future__ import annotations

import logging

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.components.ffmpeg import async_get_image as ffmpeg_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_PORT,
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DOMAIN,
    build_rtsp_url,
    uses_builtin_backend,
)
from .coordinator import PhilipsAventCoordinator
from .entity import build_device_info
from .frame_cache import FrameCache
from .restream import Restreamer

_LOGGER = logging.getLogger(__name__)

#: How long a dashboard thumbnail may lag reality. Attempts, not successes,
#: are throttled — see frame_cache.py. 60 s turns the 10 s poll's ~360
#: sessions/hour into at most 60, and the stream server's
#: disconnect-on-replacement keeps snapshot churn to at most one occupied
#: pool slot either way.
SNAPSHOT_TTL = 60.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    builtin = uses_builtin_backend(entry.options)
    bridge_port = entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
    bridge_host = entry.options.get(CONF_BRIDGE_HOST, DEFAULT_BRIDGE_HOST)

    async_add_entities(
        AventBuiltinCamera(coordinator, cam_id, data["restreamer"])
        if builtin
        else AventCamera(
            coordinator,
            cam_id,
            build_rtsp_url(bridge_host, bridge_port, coordinator.camera_name, cam_id),
        )
        for cam_id, coordinator in data["coordinators"].items()
    )


class AventCamera(Camera):
    """The add-on backend's camera, and the shared base for the builtin one.

    Add-on: the bridge's RTSP URL from stream_source(), ffmpeg stills, and
    live view through HA's go2rtc WebRTC provider.
    """

    _attr_has_entity_name = True
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: PhilipsAventCoordinator,
        cam_id: str,
        stream_url: str,
    ):
        super().__init__()
        self.coordinator = coordinator
        self._cam_id = cam_id
        self._stream_url = stream_url
        self._frame_cache: FrameCache | None = None
        self._attr_unique_id = f"{cam_id}_camera"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    async def stream_source(self) -> str:
        return self._stream_url

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """A single JPEG frame, required by the `camera.snapshot` service.

        Builtin backend: served from a TTL cache so the dashboard's 10 s
        still poll cannot open a Tuya session per poll (see
        AventBuiltinCamera). The frame may be up to SNAPSHOT_TTL old.

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


class AventBuiltinCamera(AventCamera):
    """Builtin backend: go2rtc's restream for HLS, native WebRTC live view.

    A separate class, not flags on AventCamera, because Home Assistant
    decides a camera is "native WebRTC" at the CLASS level — by whether
    its type overrides async_handle_async_webrtc_offer — and a native
    camera is never given a WebRTC provider. The override below must
    therefore not exist on the add-on cameras, whose live view IS the
    go2rtc provider.
    """

    def __init__(
        self,
        coordinator: PhilipsAventCoordinator,
        cam_id: str,
        restreamer: Restreamer,
    ):
        # The base's _stream_url is its ffmpeg-stills fallback; the frame
        # cache below intercepts that path, so no URL is needed here.
        super().__init__(coordinator, cam_id, stream_url="")
        self._restreamer = restreamer
        self._frame_cache = FrameCache(self._fetch_still, ttl=SNAPSHOT_TTL)

    @property
    def use_stream_for_stills(self) -> bool:
        """Never route stills through Home Assistant's stream component.

        True would hold an open RTSP consumer on the producer for as long
        as thumbnails are polled — an idle dashboard would keep the camera
        streaming around the clock, keep_stream_running by accident.
        Stills come from a TTL-cached go2rtc frame grab instead
        (Restreamer.snapshot): free while the producer is hot, one Tuya
        session per cache miss when it is cold.
        """
        return False

    async def stream_source(self) -> str:
        return await self._restreamer.stream_url(self._cam_id)

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Negotiate the producer stream over WHEP — one hop, native PCMU —
        as frigate-hass-integration does. Overriding this method is what
        makes the camera native: no provider exists, so there is no
        fallback — a failure is reported to the frontend, and live view is
        down until go2rtc recovers."""
        answer = await self._restreamer.whep_answer(self._cam_id, offer_sdp)
        if answer is None:
            send_message(
                WebRTCError("webrtc_offer_failed", "go2rtc could not answer the stream")
            )
            return
        send_message(WebRTCAnswer(answer))

    async def async_on_webrtc_candidate(self, session_id: str, candidate) -> None:
        """WHEP returned a complete answer — no trickle, candidates are
        noise. The base would raise on a provider-less camera, and the
        frontend sends its local candidates regardless."""

    async def _fetch_still(self) -> bytes | None:
        """One frame via go2rtc's frame handler on the producer stream; the
        cache calls this at most once per TTL."""
        return await self._restreamer.snapshot(self._cam_id)
