"""Sensor entities for Philips Avent Baby Monitor."""
from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, DPS_SENSEIQ_STATUS, DPS_SLEEP_SESSION, DPS_TEMPERATURE
from .coordinator import PhilipsAventCoordinator
from .entity import build_device_info
from .senseiq import (
    decode_senseiq_payload,
    session_attributes,
    session_duration,
    session_start,
    status_attributes,
    status_code,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for cam_id, coordinator in data["coordinators"].items():
        entities.append(AventTemperatureSensor(coordinator, cam_id))
        entities.append(AventWifiSignalSensor(coordinator, cam_id))
        entities.append(AventSenseIQStatusSensor(coordinator, cam_id))
        entities.append(AventSleepSessionStartSensor(coordinator, cam_id))
        entities.append(AventSleepSessionDurationSensor(coordinator, cam_id))
    async_add_entities(entities)


class AventTemperatureSensor(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_has_entity_name = True
    _attr_name = "Temperature"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_temperature"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def native_value(self) -> float | None:
        dps = self.coordinator.data
        if dps and DPS_TEMPERATURE in dps:
            return dps[DPS_TEMPERATURE] / 100.0
        return None


class AventWifiSignalSensor(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_has_entity_name = True
    _attr_name = "WiFi Signal"
    _attr_icon = "mdi:wifi"
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_wifi_signal"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def native_value(self) -> int | None:
        if hasattr(self.coordinator, "rssi"):
            return self.coordinator.rssi
        return None


class AventSenseIQStatusSensor(CoordinatorEntity, SensorEntity):
    """The instantaneous SenseIQ reading (DPS 3), shown as the raw device code.

    The state is the monitor's own letter code (only "b" observed so far), NOT
    a translated asleep/awake value: the vocabulary is undecoded, and guessing
    about a baby's sleep is the one thing this entity must never do. The rest
    of the payload (e.g. `br`, seen moving between 29 and 33) rides along as
    attributes verbatim, so history collects the material to decode it later.
    """

    _attr_has_entity_name = True
    _attr_name = "SenseIQ Status"
    _attr_icon = "mdi:sleep"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_senseiq_status"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def _payload(self) -> dict | None:
        dps = self.coordinator.data or {}
        return decode_senseiq_payload(dps.get(DPS_SENSEIQ_STATUS))

    @property
    def native_value(self) -> str | None:
        return status_code(self._payload)

    @property
    def extra_state_attributes(self) -> dict:
        return status_attributes(self._payload)


class AventSleepSessionStartSensor(CoordinatorEntity, SensorEntity):
    """When the current sleep session began (DPS 4 `st`, epoch seconds)."""

    _attr_has_entity_name = True
    _attr_name = "Sleep Session Start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:bed-clock"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_sleep_session_start"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def native_value(self) -> datetime | None:
        dps = self.coordinator.data or {}
        stamp = session_start(decode_senseiq_payload(dps.get(DPS_SLEEP_SESSION)))
        if stamp is None:
            return None
        return datetime.fromtimestamp(stamp, tz=UTC)


class AventSleepSessionDurationSensor(CoordinatorEntity, SensorEntity):
    """How long the current sleep session has been running (DPS 4 `sd`).

    Verified against the wall clock on live hardware: `sd` grew by exactly the
    time between two polls. The undecoded per-state timeline (`css`, `cssd`,
    `ssd` — whose durations sum to `sd` on every observed sample) is exposed as
    raw attributes rather than interpreted.
    """

    _attr_has_entity_name = True
    _attr_name = "Sleep Session Duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_sleep_session_duration"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def _payload(self) -> dict | None:
        dps = self.coordinator.data or {}
        return decode_senseiq_payload(dps.get(DPS_SLEEP_SESSION))

    @property
    def native_value(self) -> int | None:
        return session_duration(self._payload)

    @property
    def extra_state_attributes(self) -> dict:
        return session_attributes(self._payload)
