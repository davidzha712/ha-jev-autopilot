"""Diagnostics: options, rooms, recent decisions. The API key is redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import JevAutopilotConfigEntry

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JevAutopilotConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "rooms": {
            room.name: {
                "status": room.status,
                "preset": room.preset_name,
                "failures": room.failures,
                "overrides_today": room.overrides_today,
                "config": dict(room.data),
                "last": room.last,
            }
            for room in runtime.rooms.values()
        },
        "calls_today": runtime.calls_today,
        "cost_today_usd": runtime.cost_today,
        "pending_confirmations": len(runtime.pending),
        "log": list(runtime.log)[-50:],
    }
