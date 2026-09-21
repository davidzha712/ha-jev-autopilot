"""Jev Autopilot: Jev decides, Home Assistant acts."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
from jevclient import JevClient

from .const import (
    CONF_BASE_URL,
    CONF_MODEL,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    REQUEST_TIMEOUT_S,
    SUBENTRY_ROOM,
)
from .controller import RoomController
from .runtime import Runtime

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

type JevAutopilotConfigEntry = ConfigEntry[Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: JevAutopilotConfigEntry) -> bool:
    """Set up the service entry and one controller per room."""
    client = JevClient(
        entry.data[CONF_API_KEY],
        session=async_get_clientsession(hass),
        base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        model=entry.data.get(CONF_MODEL, DEFAULT_MODEL),
        timeout=REQUEST_TIMEOUT_S,
    )
    runtime = Runtime(hass, entry, client)
    await runtime.async_load()
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_ROOM:
            runtime.rooms[subentry.subentry_id] = RoomController(hass, runtime, subentry)
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for room in runtime.rooms.values():
        room.async_start()

    async def _on_action(event: Event) -> None:
        await runtime.async_handle_action(event)

    entry.async_on_unload(
        hass.bus.async_listen("mobile_app_notification_action", _on_action)
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    async def _release_orphans(_hass: HomeAssistant) -> None:
        await runtime.async_release_orphans()

    entry.async_on_unload(async_at_started(hass, _release_orphans))
    return True


async def _async_reload(hass: HomeAssistant, entry: JevAutopilotConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JevAutopilotConfigEntry) -> bool:
    """Stop the rooms, give automations back, persist state."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = entry.runtime_data
        for room in runtime.rooms.values():
            await room.async_stop(release=True)
        await runtime.async_flush()
    return unloaded
