"""Per-room autopilot switch. Off hands the room back to your automations."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import JevAutopilotConfigEntry
from .entity import RoomEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JevAutopilotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, room in entry.runtime_data.rooms.items():
        async_add_entities([AutopilotSwitch(room)], config_subentry_id=subentry_id)


class AutopilotSwitch(RoomEntity, SwitchEntity, RestoreEntity):
    """On by default; the state survives restarts."""

    def __init__(self, room: Any) -> None:
        super().__init__(room, "autopilot")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        await self.room.async_set_enabled(last is None or last.state != STATE_OFF)

    @property
    def is_on(self) -> bool:
        return self.room.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.room.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.room.async_set_enabled(False)
