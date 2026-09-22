"""Setup, reauth, reconfigure, options and the room subentry flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from jevclient import JevAuthError, JevConnectionError

from custom_components.jev_autopilot.const import (
    CONF_BASE_URL,
    CONF_BRIGHTNESS_LEVELS,
    CONF_BUDGET,
    CONF_COLOR_TEMP_LEVELS,
    CONF_CONFIRM_ENTITIES,
    CONF_CONFIRM_THRESHOLD,
    CONF_HEATING_LEVELS,
    CONF_LIGHTS,
    CONF_MODEL,
    CONF_PATROL,
    CONF_PRESET,
    CONF_SWITCHES,
    CONF_YIELD,
    DOMAIN,
    SUBENTRY_ROOM,
)

from .conftest import API_KEY, make_entry, room

USER_INPUT = {
    CONF_API_KEY: API_KEY,
    CONF_BASE_URL: "https://openrouter.ai/api/",
    CONF_MODEL: "typesafe/jev-1.13",
}


async def test_user_flow_creates_entry(hass: HomeAssistant, jev) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == "https://openrouter.ai/api"
    assert len(jev.calls) == 1


async def test_user_flow_errors_then_recovers(hass: HomeAssistant, jev) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    jev.error = JevAuthError("bad key")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "invalid_auth"}
    jev.error = JevConnectionError("down")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}
    jev.error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(hass: HomeAssistant, jev) -> None:
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth_replaces_key(hass: HomeAssistant, jev) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new-key"}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reconfigure_keeps_key_when_blank(hass: HomeAssistant, jev) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: "https://other.invalid/", CONF_MODEL: "m2"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_KEY] == API_KEY
    assert entry.data[CONF_BASE_URL] == "https://other.invalid"
    assert entry.data[CONF_MODEL] == "m2"


async def test_options_validate_levels(hass: HomeAssistant, jev) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    base = {
        CONF_PRESET: "conservative",
        CONF_CONFIRM_THRESHOLD: 0.9,
        CONF_BUDGET: 500,
        CONF_PATROL: 10,
        CONF_BRIGHTNESS_LEVELS: "10, 50, 100",
        CONF_COLOR_TEMP_LEVELS: "2700, 4000",
        CONF_HEATING_LEVELS: "off, 18, 21",
    }
    bad = {**base, CONF_BRIGHTNESS_LEVELS: "bright"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)
    assert result["errors"] == {CONF_BRIGHTNESS_LEVELS: "invalid_levels"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], base)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PRESET] == "conservative"


async def test_room_subentry_create_and_errors(hass: HomeAssistant, jev) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ROOM), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Kitchen"}
    )
    assert result["errors"] == {"base": "no_entities"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Kitchen",
            CONF_SWITCHES: ["switch.a"],
            CONF_CONFIRM_ENTITIES: ["switch.a"],
        },
    )
    assert result["errors"] == {"base": "duplicate_entity"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Kitchen", CONF_LIGHTS: ["light.a"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (sub,) = entry.subentries.values()
    assert sub.title == "Kitchen"


async def test_room_subentry_reconfigure(hass: HomeAssistant, jev) -> None:
    entry = make_entry(room("Kitchen", **{CONF_LIGHTS: ["light.a"]}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (sub,) = entry.subentries.values()
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Cooking", CONF_LIGHTS: ["light.a", "light.b"]}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    sub = entry.subentries[sub.subentry_id]
    assert sub.title == "Cooking"
    assert sub.data[CONF_LIGHTS] == ["light.a", "light.b"]
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_room_cannot_take_another_rooms_device(hass: HomeAssistant, jev) -> None:
    entry = make_entry(room("Kitchen", **{CONF_LIGHTS: ["light.a"]}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ROOM), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Hall", CONF_LIGHTS: ["light.a"]}
    )
    assert result["errors"] == {"base": "entity_in_other_room"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Hall", CONF_SWITCHES: ["switch.b"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_room_cannot_yield_another_rooms_automation(
    hass: HomeAssistant, jev
) -> None:
    entry = make_entry(
        room("Kitchen", **{CONF_LIGHTS: ["light.a"], CONF_YIELD: ["automation.x"]})
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ROOM), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Hall", CONF_LIGHTS: ["light.b"], CONF_YIELD: ["automation.x"]},
    )
    assert result["errors"] == {"base": "automation_in_other_room"}
