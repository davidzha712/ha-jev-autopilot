"""The text Jev reads: enough situation to decide, no identities."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.jev_autopilot.state import build_state, scrub, snapshot


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
        context=["sensor.lux", "binary_sensor.motion", "person.alice"],
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


async def test_malformed_numbers_are_dropped(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "light.odd",
        "on",
        {"supported_color_modes": ["brightness"], "brightness": "n/a"},
    )
    hass.states.async_set("climate.odd", "heat", {"temperature": None})
    light = snapshot(hass, "light.odd", confirm=False)
    assert light is not None and light.brightness_pct is None
    trv = snapshot(hass, "climate.odd", confirm=False)
    assert trv is not None and trv.target_temp is None
    assert "brightness" not in build_state(
        hass,
        room="R",
        controlled=["light.odd"],
        context=[],
        overrides={},
        house_notes="",
        room_notes="",
    )


async def test_resident_names_and_addresses_are_scrubbed(hass: HomeAssistant) -> None:
    hass.states.async_set("person.alice", "home", {"friendly_name": "Alice"})
    hass.states.async_set(
        "light.lamp", "on", {"friendly_name": "Alice's lamp", "brightness": 255}
    )
    hass.states.async_set("sensor.wan", "203.0.113.7", {"friendly_name": "Router"})
    text = build_state(
        hass,
        room="R",
        controlled=["light.lamp"],
        context=["sensor.wan"],
        overrides={},
        house_notes="Alice works from home.",
        room_notes="",
    )
    assert "Alice" not in text
    assert "203.0.113.7" not in text
    assert "a resident's lamp" in text
    assert "Router: [address]" in text


def test_scrub_matches_cjk_and_any_case() -> None:
    assert scrub("张三的卧室", ["张三"]) == "a resident的卧室"
    assert scrub("ALICE and alice", ["Alice"]) == "a resident and a resident"
    # A name inside a longer Latin word is left alone.
    assert scrub("Alicent", ["Alice"]) == "Alicent"


def test_scrub_addresses_next_to_letters_and_cjk() -> None:
    for text in ("打印机192.0.2.20", "nas_192.0.2.5", "host192.0.2.5."):
        assert "192.0.2" not in scrub(text, [])
    assert scrub("version 1.2.3.4.5", []) == "version 1.2.3.4.5"


def test_scrub_known_entity_ids_only() -> None:
    text = "last: light.bedroom_lamp, temp 21.5, see example.com"
    assert scrub(text, [], {"light.bedroom_lamp"}) == (
        "last: a device, temp 21.5, see example.com"
    )


async def test_name_parts_and_users_are_scrubbed(hass: HomeAssistant) -> None:
    hass.states.async_set("person.alice", "home", {"friendly_name": "Alice Smith"})
    hass.states.async_set("sensor.last", "light.lamp", {"friendly_name": "Last used"})
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})
    text = build_state(
        hass,
        room="R",
        controlled=[],
        context=["sensor.last"],
        overrides={},
        house_notes="Alice works here; so does Bob.",
        room_notes="",
        extra_names=["Bob"],
    )
    for leak in ("Alice", "Bob", "light.lamp"):
        assert leak not in text
