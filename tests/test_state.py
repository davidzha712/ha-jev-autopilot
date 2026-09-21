"""The text Jev reads: enough situation to decide, no identities."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.jev_autopilot.state import build_state, snapshot


async def test_snapshot_light_and_climate(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "light.ceiling",
        "on",
        {
            "friendly_name": "Ceiling",
            "supported_color_modes": ["color_temp"],
            "brightness": 128,
            "color_temp_kelvin": 3000,
            "min_color_temp_kelvin": 2200,
            "max_color_temp_kelvin": 6500,
        },
    )
    hass.states.async_set(
        "climate.trv",
        "heat",
        {"friendly_name": "Radiator", "temperature": 21, "hvac_modes": ["off", "heat"]},
    )
    light = snapshot(hass, "light.ceiling", confirm=False)
    assert light is not None
    assert light.supports_brightness and light.supports_color_temp
    assert light.brightness_pct == 50.2
    assert light.color_temp_k == 3000
    trv = snapshot(hass, "climate.trv", confirm=True)
    assert trv is not None and trv.confirm
    assert trv.target_temp == 21.0
    assert trv.hvac_modes == ("off", "heat")
    assert snapshot(hass, "light.missing", confirm=False) is None


async def test_build_state_has_situation_but_no_ids_or_people(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sun.sun", "below_horizon")
    hass.states.async_set(
        "weather.home", "rainy", {"temperature": 7, "temperature_unit": "°C"}
    )
    hass.states.async_set("person.alice", "home", {"friendly_name": "Alice"})
    hass.states.async_set("person.bob", "not_home", {"friendly_name": "Bob"})
    hass.states.async_set(
        "sensor.lux",
        "12",
        {"friendly_name": "Brightness", "unit_of_measurement": "lx"},
    )
    hass.states.async_set(
        "binary_sensor.motion",
        "on",
        {"friendly_name": "Motion", "device_class": "motion"},
    )
    hass.states.async_set(
        "light.ceiling",
        "on",
        {"friendly_name": "Ceiling", "brightness": 255, "color_temp_kelvin": 2700},
    )
    hass.states.async_set(
        "climate.trv",
        "heat",
        {"friendly_name": "Radiator", "current_temperature": 19, "temperature": 21},
    )
    text = build_state(
        hass,
        room="Living room",
        controlled=["light.ceiling", "climate.trv", "light.gone"],
        context=["sensor.lux", "binary_sensor.motion"],
        overrides={"light.ceiling": dt_util.now().timestamp() - 600},
        house_notes="Two adults.",
        room_notes="Reading corner.",
    )
    for fact in (
        "Room: Living room.",
        "Sun: below the horizon.",
        "Weather outside: rainy, 7 °C.",
        "Residents at home: 1 of 2.",
        "- Brightness: 12 lx",
        "- Motion: on, (motion)",
        "- Ceiling: on, brightness 100%, 2700K",
        "room 19 °C, target 21 °C",
        "- Ceiling set to on 10 min ago",
        "About the house: Two adults.",
        "About this room: Reading corner.",
    ):
        assert fact in text, fact
    for leak in ("Alice", "Bob", "light.ceiling", "sensor.lux", "person."):
        assert leak not in text, leak


async def test_build_state_minimal(hass: HomeAssistant) -> None:
    text = build_state(
        hass,
        room="Hall",
        controlled=[],
        context=[],
        overrides={"light.x": 0.0},
        house_notes=" ",
        room_notes="",
    )
    assert text.splitlines()[1] == "Room: Hall."
    assert len(text.splitlines()) == 2
