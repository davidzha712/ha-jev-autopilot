"""Per-room preset: how eager the room is to act on Jev's answers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import JevAutopilotConfigEntry
from .engine import PRESETS
from .entity import RoomEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JevAutopilotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, room in entry.runtime_data.rooms.items():
        async_add_entities([PresetSelect(room)], config_subentry_id=subentry_id)


class PresetSelect(RoomEntity, SelectEntity, RestoreEntity):
    """Starts from the global preset in the options, remembers per-room changes."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, room: Any) -> None:
        self._attr_options = list(PRESETS)
        super().__init__(room, "preset")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in PRESETS:
            self.room.set_preset(last.state)

    @property
    def current_option(self) -> str:
        return self.room.preset_name

    async def async_select_option(self, option: str) -> None:
        self.room.set_preset(option)
