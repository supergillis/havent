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
    SENSING_STATUSES,
    SLEEP_STATES,
    breathing_rate,
    decode_senseiq_payload,
    sensing_status,
    session_attributes,
    session_duration,
    session_start,
    sleep_state,
    status_attributes,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for cam_id, coordinator in data["coordinators"].items():
        entities.append(AventTemperatureSensor(coordinator, cam_id))
        entities.append(AventWifiSignalSensor(coordinator, cam_id))
        entities.append(AventSleepStateSensor(coordinator, cam_id))
        entities.append(AventBreathingRateSensor(coordinator, cam_id))
        entities.append(AventSensingStatusSensor(coordinator, cam_id))
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


class AventSleepStateSensor(CoordinatorEntity, SensorEntity):
    """The baby's current sleep state (DPS 4 `css`), translated.

    `d` = deep sleep, confirmed against the Philips app on live hardware;
    `l` = light sleep, inferred from the alternation of the segment timeline
    (see senseiq.py). This is the entity people automate on, so it is a strict
    ENUM: an unrecognised code reads unknown — senseiq.sleep_state returns
    None, which HA passes through before it ever reaches the options check —
    rather than leaking a raw letter that would look like a real state.

    Carries the raw state timeline as attributes (`current_state` is the
    untranslated letter, `current_state_duration` seconds in it, and
    `state_segments` the completed earlier segments), since the timeline is
    about states, not about the session duration it previously rode on.
    """

    _attr_has_entity_name = True
    _attr_name = "Sleep State"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SLEEP_STATES
    _attr_icon = "mdi:sleep"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_sleep_state"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def _payload(self) -> dict | None:
        dps = self.coordinator.data or {}
        return decode_senseiq_payload(dps.get(DPS_SLEEP_SESSION))

    @property
    def native_value(self) -> str | None:
        return sleep_state(self._payload)

    @property
    def extra_state_attributes(self) -> dict:
        return session_attributes(self._payload)


class AventBreathingRateSensor(CoordinatorEntity, SensorEntity):
    """The baby's breathing rate (DPS 3 `br`), in breaths per minute.

    Confirmed against the app's own breathing-rate display: 27, 28, 30 and 33
    all matched. HA has no device class for respiratory rate, so this is a
    plain measurement with an explicit unit.
    """

    _attr_has_entity_name = True
    _attr_name = "Breathing Rate"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "breaths/min"
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:lungs"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_breathing_rate"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def native_value(self) -> float | None:
        dps = self.coordinator.data or {}
        return breathing_rate(decode_senseiq_payload(dps.get(DPS_SENSEIQ_STATUS)))


class AventSensingStatusSensor(CoordinatorEntity, SensorEntity):
    """What SenseIQ is currently sensing (DPS 3 `r`), translated.

    The live sensing status — a different axis from the Sleep State sensor's
    DPS 4 stage: this says what the camera reads off the crib right now, not
    how deeply the baby sleeps. The documented value set (APK strings of the
    same Baby Monitor+ app, github.com/eisbaw/babymonitor-client) is moving /
    breathing / no-signal / out-of-crib / analyzing; `b` = "breathing" and
    `m` = "movement" are pinned so far (evidence in SENSING_STATUS_CODES;
    `b` held for an hour of live sampling while the stage cycled, `m` was
    caught by this sensor's own unknown-plus-raw-attribute discipline and
    paired with the app showing "Movement"). It is a real
    signal, not a diagnostic, so it sits with the other sensors. Same strict
    ENUM discipline as Sleep State: an unobserved code reads unknown —
    senseiq.sensing_status returns None, which HA passes through before the
    options check — rather than leaking a letter as a state. The raw code
    (`status_code`) and the rest of the payload ride along as attributes, so
    the day another letter shows up it is caught, not lost.
    """

    _attr_has_entity_name = True
    _attr_name = "Sensing Status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SENSING_STATUSES
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_sensing_status"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def _payload(self) -> dict | None:
        dps = self.coordinator.data or {}
        return decode_senseiq_payload(dps.get(DPS_SENSEIQ_STATUS))

    @property
    def native_value(self) -> str | None:
        return sensing_status(self._payload)

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
    time between two polls, and equals the sum of the segment timeline plus
    `cssd` on every observed sample. The timeline itself lives on the Sleep
    State sensor's attributes now, next to the state it describes.
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
    def native_value(self) -> int | None:
        dps = self.coordinator.data or {}
        return session_duration(decode_senseiq_payload(dps.get(DPS_SLEEP_SESSION)))
