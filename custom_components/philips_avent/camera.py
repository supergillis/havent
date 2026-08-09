"""Camera entity for Philips Avent Baby Monitor."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image as ffmpeg_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_PORT,
    CONF_SIGNALING_PORT,
    CONF_STREAM_TOKEN,
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_SIGNALING_PORT,
    DOMAIN,
    build_rtsp_url,
    builtin_stream_url,
    uses_builtin_backend,
)
from .coordinator import PhilipsAventCoordinator
from .entity import build_device_info
from .frame_cache import FrameCache

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
        AventCamera(
            coordinator,
            cam_id,
            builtin_stream_url(
                entry.options.get(CONF_SIGNALING_PORT, DEFAULT_SIGNALING_PORT),
                entry.data.get(CONF_STREAM_TOKEN, ""),
                cam_id,
            )
            if builtin
            else build_rtsp_url(bridge_host, bridge_port, coordinator.camera_name, cam_id),
            builtin=builtin,
        )
        for cam_id, coordinator in data["coordinators"].items()
    )


class AventCamera(Camera):
    """Camera entity fed by whichever backend this entry uses."""

    _attr_has_entity_name = True
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: PhilipsAventCoordinator,
        cam_id: str,
        stream_url: str,
        *,
        builtin: bool = False,
    ):
        super().__init__()
        self.coordinator = coordinator
        self._cam_id = cam_id
        self._stream_url = stream_url
        self._frame_cache = FrameCache(self._fetch_still, ttl=SNAPSHOT_TTL) if builtin else None
        self._attr_unique_id = f"{cam_id}_camera"
        self._attr_device_info = build_device_info(coordinator, cam_id)

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

    async def stream_source(self) -> str:
        return self._stream_url

    async def _fetch_still(self) -> bytes | None:
        """One frame via go2rtc's frame path; the cache calls this at most once per TTL."""
        if (provider := self.webrtc_provider) is None:
            return None
        return await provider.async_get_image(self)

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
