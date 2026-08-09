"""Driving go2rtc's stream preload for the built-in backend.

The `keep_stream_running` option promises the Go bridge's old behaviour: one
long-lived Tuya session, always connected, instead of one per go2rtc dial.
The lever is go2rtc's own preload API (`PUT /api/preload`): a preloaded
stream keeps a permanent internal consumer attached, so the producer — our
signaling endpoint, hence the Tuya session — is dialled once and never
stopped. Home Assistant's own `preload_stream` camera preference is NOT the
lever: it feeds `stream_source()` to an ffmpeg/HLS pipeline, which cannot
open this backend's `webrtc:ws://` URL and logs "Protocol not found",
forever — and it would mean writing another integration's user preference.

go2rtc only knows a stream once HA's go2rtc provider has registered it,
which happens on the first dial, so there are two arming paths:

- `CameraSource.on_answered` (stream_server.py stays HA-free; __init__.py
  passes `camera_answered` in). By then go2rtc necessarily knows the
  stream — go2rtc is what dialled us. Every answer re-arms, because a
  preload can stop doing its job behind our back: go2rtc restarted, or
  HA's provider disabled it (it turns preload off for a camera whose
  `preload_stream` preference is unset, on entity register/unregister and
  on any camera-preferences update).
- `async_resume` at entry setup re-enables preload for streams go2rtc
  still knows, so the option survives a reload. After a full HA restart
  go2rtc starts empty; the stream goes hot at the first view or thumbnail.

Arming MUST check before it PUTs, because go2rtc's `PUT /api/preload` is
destructive (internal/streams/preload.go, AddPreload): a PUT for an
already-preloaded stream drops the live preload consumer — stopping the
producer — and synchronously redials it. A blind PUT on every answer is a
feedback loop: it tears down the session that just answered, which dials,
which answers, which PUTs again, churning Tuya sessions against a camera
with a 3-5 slot pool. So `_async_enable` checks `GET /api/preload` first,
and a lock keeps a burst of answers from racing that check.

Everything degrades quietly: no managed go2rtc (HA Core in a venv without
`go2rtc: url:`), a missing go2rtc_client package, or an erroring API log
once per entry and never break the camera entity or the setup.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

try:
    from .const import go2rtc_stream_name
except ImportError:  # imported outside the package, e.g. by the tests
    from const import go2rtc_stream_name

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

try:
    from go2rtc_client import Go2RtcRestClient
except ImportError:  # go2rtc integration (and its requirement) not installed
    Go2RtcRestClient = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)

#: Where HA's go2rtc integration parks its `Go2RtcConfig(url, session)`.
#: Its own HassKey is private, but a HassKey is just a typed str and
#: hass.data is keyed by value, so the plain domain name reads the same slot.
_GO2RTC_DATA = "go2rtc"

_NO_GO2RTC = (
    "keep_stream_running is on, but Home Assistant's go2rtc is not "
    "available; the stream will only run while someone is watching"
)


def describe_error(err: BaseException) -> str:
    """A log-worthy account of an exception whose str() may be empty.

    go2rtc_client raises a *bare* `Go2RtcClientError from exc` and aiohttp's
    total timeout a bare `TimeoutError` — both str() "", so logging `{err}`
    printed literally nothing. Render type names and the cause chain instead.
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = err
    while cur is not None and id(cur) not in seen and len(parts) < 5:
        seen.add(id(cur))
        text = str(cur)
        name = type(cur).__name__
        parts.append(f"{name}: {text}" if text else name)
        cur = cur.__cause__
    return " <- caused by ".join(parts)


class StreamPreloader:
    """Drives go2rtc's preload for one config entry's cameras."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._warned = False
        self._hot: set[str] = set()  # cameras we have already logged as hot
        self._lock = asyncio.Lock()  # serializes the check-then-PUT

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

    def camera_answered(self, camera_id: str) -> None:
        """CameraSource.on_answered: arm the preload, off the signaling path."""
        self._hass.async_create_background_task(
            self._async_enable(camera_id), f"philips_avent preload {camera_id}"
        )

    async def _async_enable(self, camera_id: str) -> None:
        if (client := self._client()) is None:
            self._complain_once(_NO_GO2RTC)
            return
        name = go2rtc_stream_name(camera_id)
        try:
            async with self._lock:
                if name in await client.preload.list():
                    # Already armed — never PUT again (see module docstring:
                    # a redundant PUT stops and redials the live producer).
                    _LOGGER.debug("go2rtc preload for %s is already armed", name)
                else:
                    await client.preload.enable(name)
        except Exception as err:  # noqa: BLE001 - go2rtc trouble must never break the camera
            self._complain_once(
                f"Could not enable go2rtc preload for {name} ({describe_error(err)}); "
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

    async def async_resume(self, camera_ids: Iterable[str]) -> None:
        """Re-arm preload for streams go2rtc already knows.

        Runs after the platforms are set up, so the camera entity's
        registration with HA's go2rtc provider — which disables a preload it
        did not ask for — has already happened and cannot race us.
        """
        if (client := self._client()) is None:
            self._complain_once(_NO_GO2RTC)
            return
        try:
            known = await client.streams.list()
        except Exception as err:  # noqa: BLE001 - go2rtc trouble must never break setup
            self._complain_once(f"Could not list go2rtc streams ({describe_error(err)})")
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
            _LOGGER.debug("Could not check or stop go2rtc preload: %s", describe_error(err))
