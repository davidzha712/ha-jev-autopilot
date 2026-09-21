"""Read Home Assistant state into engine snapshots and into the text Jev reads.

The text never contains entity ids or people's names: Jev needs the situation,
not the identities, and the state leaves the house.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .engine import UNUSABLE_STATES, EntitySnapshot

# Named after people. Only the count of residents at home is ever sent.
_PERSONAL_DOMAINS = {"person", "device_tracker"}

_BRIGHTNESS_MODES = {
    "brightness",
    "color_temp",
    "hs",
    "xy",
    "rgb",
    "rgbw",
    "rgbww",
    "white",
}


def snapshot(
    hass: HomeAssistant, entity_id: str, *, confirm: bool
) -> EntitySnapshot | None:
    """Engine view of one entity, or None when it does not exist."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    attrs = state.attributes
    domain = state.domain
    kwargs: dict[str, object] = {}
    if domain == "light":
        modes = set(attrs.get("supported_color_modes") or [])
        kwargs["supports_brightness"] = bool(modes & _BRIGHTNESS_MODES)
        kwargs["supports_color_temp"] = "color_temp" in modes
        if (bri := _number(attrs.get("brightness"))) is not None:
            kwargs["brightness_pct"] = round(bri / 255 * 100, 1)
        if (kelvin := _number(attrs.get("color_temp_kelvin"))) is not None:
            kwargs["color_temp_k"] = int(kelvin)
        kwargs["min_color_temp_k"] = attrs.get("min_color_temp_kelvin")
        kwargs["max_color_temp_k"] = attrs.get("max_color_temp_kelvin")
    elif domain == "climate":
        if (target := _number(attrs.get("temperature"))) is not None:
            kwargs["target_temp"] = target
        kwargs["hvac_modes"] = tuple(str(m) for m in attrs.get("hvac_modes") or ())
    return EntitySnapshot(
        entity_id=entity_id,
        name=state.name,
        state=state.state,
        confirm=confirm,
        **kwargs,  # type: ignore[arg-type]
    )


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ago(now: datetime, then: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def _describe(state: State, now: datetime) -> str:
    attrs = state.attributes
    parts = [state.state]
    if state.state not in UNUSABLE_STATES:
        if unit := attrs.get("unit_of_measurement"):
            parts[0] = f"{state.state} {unit}"
        if dc := attrs.get("device_class"):
            parts.append(f"({dc})")
        if state.domain == "light" and state.state == "on":
            if (bri := _number(attrs.get("brightness"))) is not None:
                parts.append(f"brightness {round(bri / 255 * 100)}%")
            if (kelvin := attrs.get("color_temp_kelvin")) is not None:
                parts.append(f"{kelvin}K")
        if state.domain == "climate":
            if (cur := attrs.get("current_temperature")) is not None:
                parts.append(f"room {cur} °C")
            if (target := attrs.get("temperature")) is not None:
                parts.append(f"target {target} °C")
    parts.append(f"changed {_ago(now, state.last_changed)}")
    return ", ".join(parts)


def build_state(
    hass: HomeAssistant,
    *,
    room: str,
    controlled: Sequence[str],
    context: Sequence[str],
    overrides: Mapping[str, float],
    house_notes: str,
    room_notes: str,
) -> str:
    """The situation as plain text, one fact per line."""
    now = dt_util.now()
    lines = [f"Local time: {now:%Y-%m-%d %H:%M}, {now:%A}.", f"Room: {room}."]

    if (sun := hass.states.get("sun.sun")) is not None:
        where = "above" if sun.state == "above_horizon" else "below"
        lines.append(f"Sun: {where} the horizon.")
    weather = _first(hass, "weather")
    if weather is not None and weather.state not in UNUSABLE_STATES:
        temp = weather.attributes.get("temperature")
        unit = weather.attributes.get("temperature_unit", "")
        extra = f", {temp} {unit}".rstrip() if temp is not None else ""
        lines.append(f"Weather outside: {weather.state}{extra}.")

    people = hass.states.async_all("person")
    if people:
        home = sum(1 for s in people if s.state == "home")
        lines.append(f"Residents at home: {home} of {len(people)}.")

    sensors = [s for s in _states(hass, context) if s.domain not in _PERSONAL_DOMAINS]
    if sensors:
        lines.append("Room sensors:")
        lines.extend(f"- {s.name}: {_describe(s, now)}" for s in sensors)
    devices = _states(hass, controlled)
    if devices:
        lines.append("Devices in this room:")
        lines.extend(f"- {s.name}: {_describe(s, now)}" for s in devices)

    recent = []
    now_ts = now.timestamp()
    for entity_id, at in overrides.items():
        if now_ts - at <= 3600 and (s := hass.states.get(entity_id)) is not None:
            recent.append(
                f"- {s.name} set to {s.state} {int((now_ts - at) // 60)} min ago"
            )
    if recent:
        lines.append("Changed by hand by a resident in the last hour:")
        lines.extend(recent)
    if house_notes.strip():
        lines.append(f"About the house: {house_notes.strip()}")
    if room_notes.strip():
        lines.append(f"About this room: {room_notes.strip()}")
    return scrub("\n".join(lines), resident_names(hass))


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def resident_names(hass: HomeAssistant) -> list[str]:
    """Names of the people Home Assistant knows about, to keep them out of prompts."""
    return [s.name for s in hass.states.async_all("person")]


def scrub(text: str, residents: Iterable[str]) -> str:
    """Last line of defence for names users put in friendly names or notes."""
    text = _IPV4.sub("[address]", text)
    for name in sorted(
        {n.strip() for n in residents if n.strip()}, key=len, reverse=True
    ):
        # ASCII-only boundaries: \b treats CJK as word characters, so "张三的卧室"
        # would never match "张三".
        text = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])",
            "a resident",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _first(hass: HomeAssistant, domain: str) -> State | None:
    states = sorted(hass.states.async_all(domain), key=lambda s: s.entity_id)
    return states[0] if states else None


def _states(hass: HomeAssistant, ids: Iterable[str]) -> list[State]:
    return [s for i in ids if (s := hass.states.get(i)) is not None]
