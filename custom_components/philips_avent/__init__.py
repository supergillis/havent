"""Philips Avent Baby Monitor integration for Home Assistant."""
from __future__ import annotations

import json
import logging
import secrets
from functools import partial
from pathlib import Path

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import PhilipsAventAPI
from .const import (
    CONF_API_HOST,
    CONF_BRIDGE_PORT,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_ECODE,
    CONF_KEEP_STREAM_RUNNING,
    CONF_PARTNER,
    CONF_SID,
    CONF_SIGNALING_PORT,
    CONF_STREAM_TOKEN,
    CONF_TALKBACK,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_KEEP_STREAM_RUNNING,
    DEFAULT_SIGNALING_PORT,
    DEFAULT_TALKBACK,
    DOMAIN,
    TUYA_APP_KEY,
    TUYA_DEFAULT_COUNTRY_CODE,
    TUYA_PACKAGE_NAME,
    TUYA_SIGNING_KEY,
    uses_builtin_backend,
)
from .coordinator import PhilipsAventCoordinator
from .payload import BRIDGE_CONFIG_PREFIX, bridge_config_filename, build_bridge_config, orphan_bridge_configs
from .preload import StreamPreloader
from .region import DEFAULT_DATA_CENTER, api_host, api_url_for_host
from .signaling import Credentials, SignalingHub
from .stream_server import CameraSource, StreamServer

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CAMERA, Platform.SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.BUTTON, Platform.SELECT, Platform.BINARY_SENSOR]


def _entry_api_host(entry: ConfigEntry) -> str:
    """API host for this account, falling back to Central Europe.

    Entries created before data-center routing existed have no host stored; EU
    is the right fallback for them because that was the only host the
    integration ever used (issues #44, #58).
    """
    return entry.data.get(CONF_API_HOST) or api_host(DEFAULT_DATA_CENTER)


async def _write_bridge_config(hass: HomeAssistant, entry: ConfigEntry, api: PhilipsAventAPI, cameras: list) -> None:
    """Write bridge config JSON for the add-on."""
    bridge_port = entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
    bridge_config = build_bridge_config(
        signing_key=TUYA_SIGNING_KEY,
        sid=entry.data[CONF_SID],
        ecode=entry.data.get(CONF_ECODE, ""),
        partner=entry.data.get(CONF_PARTNER, ""),
        app_key=TUYA_APP_KEY,
        device_id=api.device_id,
        package_name=TUYA_PACKAGE_NAME,
        api_host=_entry_api_host(entry),
        talkback=entry.options.get(CONF_TALKBACK, DEFAULT_TALKBACK),
        bridge_port=bridge_port,
        cameras=cameras,
    )
    bridge_path = Path(hass.config.path(bridge_config_filename(entry.entry_id)))
    await hass.async_add_executor_job(
        bridge_path.write_text, json.dumps(bridge_config, indent=2)
    )
    _LOGGER.info(
        "Bridge config written to %s (port: %d, api host: %s)",
        bridge_path, bridge_port, bridge_config["api_host"],
    )

    legacy_path = Path(hass.config.path("philips_avent_bridge.json"))
    if await hass.async_add_executor_job(legacy_path.exists):
        await hass.async_add_executor_job(legacy_path.unlink)
        _LOGGER.info("Removed legacy bridge config %s", legacy_path)

    await _remove_orphan_bridge_configs(hass)


async def _remove_bridge_config(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Delete this entry's bridge config file, if there is one."""
    bridge_path = Path(hass.config.path(bridge_config_filename(entry.entry_id)))

    def _unlink() -> bool:
        try:
            bridge_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as err:
            _LOGGER.warning("Could not remove bridge config %s: %s", bridge_path, err)
            return False
        return True

    if await hass.async_add_executor_job(_unlink):
        _LOGGER.info("Removed bridge config %s", bridge_path)
        return True
    return False


async def _remove_orphan_bridge_configs(hass: HomeAssistant) -> None:
    """Delete bridge config files belonging to entries that no longer exist.

    Re-adding the integration mints a new entry id, so the previous file stayed
    behind and the add-on could keep reading it: old session, old camera id, and
    a Tuya "No access" on every stream attempt (issue #52). Reinstalling made it
    worse, since each attempt left one more file.
    """
    config_dir = Path(hass.config.path())
    valid = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}

    def _prune() -> list[str]:
        names = [p.name for p in config_dir.glob(f"{BRIDGE_CONFIG_PREFIX}*.json")]
        removed = []
        for name in orphan_bridge_configs(names, valid):
            try:
                (config_dir / name).unlink()
            except OSError as err:
                _LOGGER.warning("Could not remove stale bridge config %s: %s", name, err)
            else:
                removed.append(name)
        return removed

    removed = await hass.async_add_executor_job(_prune)
    for name in removed:
        _LOGGER.info(
            "Removed stale bridge config %s, it belonged to a config entry that no longer exists",
            name,
        )


def stream_token(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """The secret in the signaling URL, minted once and kept.

    Persisted rather than regenerated so the URL handed to go2rtc survives a
    restart unchanged.
    """
    if token := entry.data.get(CONF_STREAM_TOKEN):
        return token
    token = secrets.token_urlsafe(16)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_STREAM_TOKEN: token}
    )
    return token


async def _async_start_streaming(
    hass: HomeAssistant,
    entry: ConfigEntry,
    api: PhilipsAventAPI,
    cameras: list,
    preloader: StreamPreloader | None,
) -> SignalingHub:
    """Serve this entry's cameras from the built-in signaling server."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    server: StreamServer | None = domain_data.get("server")
    if server is None:
        server = StreamServer(
            entry.options.get(CONF_SIGNALING_PORT, DEFAULT_SIGNALING_PORT)
        )
        await server.start()
        domain_data["server"] = server
    elif server.port != entry.options.get(CONF_SIGNALING_PORT, DEFAULT_SIGNALING_PORT):
        _LOGGER.warning(
            "Another config entry already started the signaling server on port %d; "
            "this entry's port setting is ignored",
            server.port,
        )

    hub = SignalingHub(
        api,
        Credentials(
            sid=entry.data[CONF_SID],
            ecode=entry.data.get(CONF_ECODE, ""),
            partner=entry.data.get(CONF_PARTNER, ""),
            device_id=api.device_id,
        ),
    )
    token = stream_token(hass, entry)
    talkback = entry.options.get(CONF_TALKBACK, DEFAULT_TALKBACK)

    for cam in cameras:
        cam_id = cam["deviceId"]
        server.add_camera(CameraSource(
            camera_id=cam_id,
            name=cam["deviceName"],
            hub=hub,
            token=token,
            talkback=talkback,
            on_auth_failed=lambda: entry.async_start_reauth(hass),
            # The keep_stream_running hook: arms go2rtc's preload once the
            # camera has answered, the earliest moment go2rtc knows the
            # stream. Passed in as a plain callback so stream_server.py
            # stays free of Home Assistant imports.
            on_answered=partial(preloader.camera_answered, cam_id) if preloader else None,
        ))
    return hub


async def _async_stop_streaming(hass: HomeAssistant, data: dict) -> None:
    """Give up this entry's cameras, and the server once nobody is left."""
    domain_data = hass.data[DOMAIN]
    server: StreamServer | None = domain_data.get("server")
    if server is not None:
        for cam_id in data["coordinators"]:
            server.remove_camera(cam_id)
        if not server.cameras:
            await server.stop()
            domain_data.pop("server", None)
    if (hub := data.get("hub")) is not None:
        await hub.close()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Avent from a config entry."""
    session = aiohttp.ClientSession()
    api = PhilipsAventAPI(
        session,
        sid=entry.data[CONF_SID],
        api_url=api_url_for_host(_entry_api_host(entry)),
        country_code=entry.data.get(CONF_COUNTRY_CODE) or TUYA_DEFAULT_COUNTRY_CODE,
        device_id=entry.data.get(CONF_DEVICE_ID),
    )
    if not entry.data.get(CONF_DEVICE_ID):
        # Entries created before the id was persisted: keep the one just
        # generated, so the bridge config stops changing on every restart
        # (issue #73).
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_ID: api.device_id}
        )
        _LOGGER.info("Stored a stable device id for this account")

    # Use cameras stored in config entry (discovered during config flow)
    cameras = []
    stored_cameras = entry.data.get("cameras", [])
    if stored_cameras:
        cameras.extend(
            {
                "deviceId": cam["id"],
                "deviceName": cam["name"],
                "productId": cam.get("product_id", ""),
            }
            for cam in stored_cameras
        )
        _LOGGER.info("Using %d cameras from config entry", len(cameras))

        # Backfill productId for entries created before this field was tracked.
        # Guard on key presence in the stored entry, NOT on the in-memory value,
        # so post-fix entries with a genuinely empty productId (e.g. a device
        # that does not expose one) don't trigger a cloud call on every restart.
        if any("product_id" not in cam for cam in stored_cameras):
            patched = 0
            try:
                discovered = await api.discover_cameras()
                by_id = {(d.get("devId") or d.get("deviceId")): d for d in discovered}
                for cam in cameras:
                    if not cam.get("productId"):
                        disc = by_id.get(cam.get("deviceId"))
                        if disc:
                            new_id = disc.get("productId") or disc.get("productKey") or ""
                            if new_id:
                                cam["productId"] = new_id
                                patched += 1
                if patched:
                    updated_stored_cameras = [
                        {**stored_cam, "product_id": cam.get("productId", "")}
                        for stored_cam, cam in zip(stored_cameras, cameras)
                    ]
                    hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, "cameras": updated_stored_cameras},
                    )
                    _LOGGER.info("Backfilled productId for %d camera(s) and persisted to config entry", patched)
                else:
                    _LOGGER.info("Backfill ran but no productId was recovered from Tuya discovery")
            except Exception:  # noqa: BLE001 - setup continues even if the backfill fails
                _LOGGER.warning(
                    "Could not backfill productId from Tuya API; SCD951 cameras may fail "
                    "to stream until HA restarts or the integration is reconfigured"
                )
    else:
        # Fallback: re-discover via API
        try:
            cameras = await api.discover_cameras()
        except Exception:
            _LOGGER.exception("Camera discovery failed")
            cameras = []

    if not cameras:
        _LOGGER.error("No cameras found. Reconfigure the integration to re-discover.")
        await session.close()
        return False

    coordinators = {}
    for cam in cameras:
        cam_id = cam.get("deviceId") or cam.get("devId")
        cam_name = cam.get("deviceName") or cam.get("name", cam_id)
        local_key = cam.get("localKey")

        coordinator = PhilipsAventCoordinator(hass, api, cam_id, cam_name, local_key=local_key)
        await coordinator.async_config_entry_first_refresh()

        if not local_key:
            local_key = coordinator.device_info.get("localKey")
            if local_key:
                coordinator._local_key = local_key

        await coordinator.start_lan()
        coordinators[cam_id] = coordinator

    hub = None
    preloader = None
    builtin = uses_builtin_backend(entry.options)
    if builtin and entry.options.get(CONF_KEEP_STREAM_RUNNING, DEFAULT_KEEP_STREAM_RUNNING):
        preloader = StreamPreloader(hass)
    if builtin:
        hub = await _async_start_streaming(hass, entry, api, cameras, preloader)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "session": session,
        "coordinators": coordinators,
        "config": entry.data,
        "hub": hub,
    }

    if builtin:
        # Leave nothing for the add-on to pick up: two backends on one account
        # would fight over the same Tuya MQTT client id.
        if await _remove_bridge_config(hass, entry):
            _LOGGER.warning(
                "Streaming moved to the built-in backend; stop the aventproxy "
                "bridge add-on, it has nothing left to serve"
            )
    else:
        await _write_bridge_config(hass, entry, api, cameras)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # keep_stream_running bookkeeping. Deliberately after the platform
    # forward: the camera entity's registration with HA's go2rtc provider
    # happens inside it, and that registration disables any preload the
    # provider did not ask for — resuming before it would lose the race.
    camera_ids = list(coordinators)
    if preloader is not None:
        entry.async_create_background_task(
            hass, preloader.async_resume(camera_ids), "philips_avent go2rtc preload resume"
        )
    else:
        # Option off (or the add-on backend): stop any preload a previous
        # configuration armed, or go2rtc keeps the camera streaming for
        # nobody. Best effort, silent when go2rtc is absent.
        entry.async_create_background_task(
            hass, StreamPreloader(hass).async_disable(camera_ids), "philips_avent go2rtc preload disable"
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await _async_stop_streaming(hass, data)
        for coordinator in data["coordinators"].values():
            await coordinator.stop_lan()
        await data["session"].close()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's bridge config when the integration is removed.

    Without this the file survived the removal, and the add-on could pick it
    over the file of whatever entry the user created next (issue #52).
    """
    await _remove_bridge_config(hass, entry)
