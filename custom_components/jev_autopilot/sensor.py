"""Sensors: what each room decided, and what the service costs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JevAutopilotConfigEntry
from .const import DOMAIN
from .controller import STATUSES
from .entity import RoomEntity
from .runtime import SIGNAL_GLOBAL, Runtime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JevAutopilotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities([CallsSensor(runtime), CostSensor(runtime)])
    for subentry_id, room in runtime.rooms.items():
        async_add_entities(
            [DecisionSensor(room), StatusSensor(room), OverridesSensor(room)],
            config_subentry_id=subentry_id,
        )


class DecisionSensor(RoomEntity, SensorEntity):
    """Summary of the last decision; the full trace is in the attributes."""

    def __init__(self, room: Any) -> None:
        super().__init__(room, "last_decision")

    @property
    def native_value(self) -> str | None:
        summary = self.room.last.get("summary")
        return summary[:255] if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {k: v for k, v in self.room.last.items() if k != "summary"}


class StatusSensor(RoomEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES

    def __init__(self, room: Any) -> None:
        super().__init__(room, "status")

    @property
    def native_value(self) -> str:
        return self.room.status


class OverridesSensor(RoomEntity, SensorEntity):
    """Manual changes today: a high number means the room is guessing wrong."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, room: Any) -> None:
        super().__init__(room, "overrides_today")

    @property
    def native_value(self) -> int:
        return self.room.overrides_today


class GlobalSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: Runtime, key: str) -> None:
        self.runtime = runtime
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry.entry_id)},
            name="Jev Autopilot",
            manufacturer="TypeSafe",
            model="Jev Autopilot",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_GLOBAL, self.async_write_ha_state)
        )


class CallsSensor(GlobalSensor):
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime, "calls_today")

    @property
    def native_value(self) -> int:
        return self.runtime.calls_today

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"budget": self.runtime.budget}


class CostSensor(GlobalSensor):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime, "cost_today")

    @property
    def native_value(self) -> float:
        return round(self.runtime.cost_today, 6)
