"""Driving go2rtc's stream preload for the built-in backend.

The `keep_stream_running` option promises the Go bridge's old behaviour: one
long-lived Tuya session, always connected, instead of one per go2rtc dial.
The lever for that is go2rtc's own preload API (`PUT /api/preload`), the
same one Home Assistant's go2rtc provider drives for its `preload_stream`
camera preference: a preloaded stream keeps a permanent internal consumer
attached, so the producer — our signaling endpoint, hence the Tuya
session — is dialled once and never stopped.

Why NOT Home Assistant's own `preload_stream` camera preference: setting it
makes camera/__init__.py (its EVENT_HOMEASSISTANT_STARTED listener) call
`camera.async_create_stream()`, `stream.add_provider("hls")` and
`stream.start()` — an ffmpeg/HLS pipeline fed from `stream_source()`. This
backend's stream source is a `webrtc:ws://…` URL ffmpeg cannot open, so
that path only logs "Error opening stream (Protocol not found)", forever.
It would also mean writing another integration's user preference from ours.

Timing: go2rtc only knows a stream once HA's go2rtc provider has registered
it, which happens on the first dial (a viewer's offer, or a snapshot
through the provider's frame path); enabling preload for a stream go2rtc
has never seen fails. Hence two arming paths:

- The primary hook is "the camera answered a negotiation":
  `CameraSource.on_answered`, fired by stream_server.py — which stays
  HA-free, so the callback is passed in from __init__.py. By then go2rtc
  necessarily knows the stream, because go2rtc is what dialled us. Every
  answer re-arms, not just the first: a dial only happens when the producer
  is down, which is exactly when the preload has stopped doing its job
  (go2rtc restarted, or HA's provider disabled it behind our back — the
  provider turns preload *off* whenever the camera entity unregisters,
  i.e. on every options reload, and on any camera-preferences update).
- On entry setup, `async_resume` re-enables preload for streams go2rtc
  still knows, so the option survives a reload without waiting for the
  next viewer. After a full HA restart go2rtc starts empty; the stream
  then goes hot at the first view or dashboard thumbnail and stays hot.

Everything degrades quietly: no managed go2rtc (HA Core in a venv without
`go2rtc: url:`), a missing go2rtc_client package, or an erroring API log
once per entry and never break the camera entity or the setup.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.core import HomeAssistant

from .const import go2rtc_stream_name

try:
    from go2rtc_client import Go2RtcRestClient
except ImportError:  # go2rtc integration (and its requirement) not installed
    Go2RtcRestClient = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)

#: Where HA's go2rtc integration parks its `Go2RtcConfig(url, session)`
#: (homeassistant/components/go2rtc/__init__.py, `_DATA_GO2RTC`). Its own
#: HassKey is private, but a HassKey is just a typed str and hass.data is
#: keyed by value, so the plain domain name reads the same slot.
_GO2RTC_DATA = "go2rtc"


class StreamPreloader:
    """Drives go2rtc's preload for one config entry's cameras."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._warned = False
        self._hot: set[str] = set()  # cameras we have already logged as hot

    def _client(self) -> Go2RtcRestClient | None:
        """The rest client for HA's go2rtc, or None when there is none."""
        config = self._hass.data.get(_GO2RTC_DATA)
        url = getattr(config, "url", None)
        session = getattr(config, "session", None)
        if Go2RtcRestClient is None or not url or session is None:
            return None
        return Go2RtcRestClient(session, url)

    def _complain_once(self, message: str) -> None:
        """One warning per entry; repeats drop to debug so the log stays calm."""
        if self._warned:
            _LOGGER.debug("%s", message)
            return
        self._warned = True
        _LOGGER.warning("%s", message)

    # -- the stream_server hook (HA glue side) -----------------------------

    def camera_answered(self, camera_id: str) -> None:
        """CameraSource.on_answered: arm the preload, off the signaling path."""
        self._hass.async_create_background_task(
            self._async_enable(camera_id), f"philips_avent preload {camera_id}"
        )

    async def _async_enable(self, camera_id: str) -> None:
        if (client := self._client()) is None:
            self._complain_once(
                "keep_stream_running is on, but Home Assistant's go2rtc is not "
                "available; the stream will only run while someone is watching"
            )
            return
        name = go2rtc_stream_name(camera_id)
        try:
            await client.preload.enable(name)
        except Exception as err:  # noqa: BLE001 - go2rtc trouble must never break the camera
            self._complain_once(
                f"Could not enable go2rtc preload for {name} ({err}); "
                "the stream will only run while someone is watching"
            )
            return
        if camera_id not in self._hot:
            self._hot.add(camera_id)
            _LOGGER.info(
                "Keeping stream %s permanently connected (go2rtc preload): the "
                "camera now streams on the LAN whether or not anyone is watching",
                name,
            )

    # -- setup-time bookkeeping --------------------------------------------

    async def async_resume(self, camera_ids: Iterable[str]) -> None:
        """Re-arm preload for streams go2rtc already knows.

        Runs after the platforms are set up, so the camera entity's
        registration with HA's go2rtc provider — which disables a preload it
        did not ask for, but only when the entity is *unregistered* — has
        already happened and cannot race us.
        """
        if (client := self._client()) is None:
            self._complain_once(
                "keep_stream_running is on, but Home Assistant's go2rtc is not "
                "available; the stream will only run while someone is watching"
            )
            return
        try:
            known = await client.streams.list()
        except Exception as err:  # noqa: BLE001 - go2rtc trouble must never break setup
            self._complain_once(f"Could not list go2rtc streams ({err})")
            return
        for camera_id in camera_ids:
            if go2rtc_stream_name(camera_id) in known:
                await self._async_enable(camera_id)
            else:
                _LOGGER.debug(
                    "go2rtc does not know %s yet; the stream goes hot at the "
                    "first view or dashboard thumbnail",
                    go2rtc_stream_name(camera_id),
                )

    async def async_disable(self, camera_ids: Iterable[str]) -> None:
        """Stop any preload armed earlier: the option is now off.

        Also runs when the backend switched back to the add-on — a stale
        preload would otherwise keep go2rtc dialling a signaling server that
        is no longer there, for nobody. Silent when go2rtc is absent: with
        no go2rtc there is nothing that could still be preloading.
        """
        if (client := self._client()) is None:
            return
        try:
            preloaded = await client.preload.list()
            for camera_id in camera_ids:
                name = go2rtc_stream_name(camera_id)
                if name in preloaded:
                    await client.preload.disable(name)
                    _LOGGER.info(
                        "Stopped the permanent stream for %s (keep_stream_running is off)",
                        name,
                    )
        except Exception as err:  # noqa: BLE001 - best effort, never break setup
            _LOGGER.debug("Could not check or stop go2rtc preload: %s", err)
