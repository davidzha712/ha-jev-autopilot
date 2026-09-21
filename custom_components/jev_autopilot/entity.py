"""Base entity: one device per room, updated through the dispatcher."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import RoomController


class RoomEntity(Entity):
    """Anything that belongs to one room."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, room: RoomController, key: str) -> None:
        self.room = room
        self._attr_translation_key = key
        self._attr_unique_id = f"{room.subentry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, room.subentry_id)},
            name=room.name,
            manufacturer="TypeSafe",
            model="Jev Autopilot room",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.room.signal, self.async_write_ha_state
            )
        )
