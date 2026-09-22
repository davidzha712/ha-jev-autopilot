"""Diagnostics never contain the API key."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.jev_autopilot.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import API_KEY, DEFAULT_ROOM, make_entry, room


async def test_diagnostics_redacts_key(hass: HomeAssistant, jev) -> None:
    entry = make_entry(room(**DEFAULT_ROOM))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["entry"]["api_key"] == "**REDACTED**"
    assert API_KEY not in str(data)
    assert "Living room" in data["rooms"]
