"""Pure decision logic: entity snapshots in, questions out; answers in, actions out.

Nothing here imports Home Assistant. The model only advises. Every threshold,
cooldown and safety rule that turns an answer into a service call lives in this
module so it can be tested without a running instance.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jevclient import (
    Answer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
)

Question = Noul | Choice | Score

UNUSABLE_STATES = frozenset({"unavailable", "unknown"})
MEDIA_ACTIVE_STATES = frozenset({"on", "playing", "paused", "idle", "buffering"})

BRIGHTNESS_DEADBAND_PCT = 15
COLOR_TEMP_DEADBAND_K = 300
COLOR_TEMP_MIN_CONFIDENCE = 0.6
CLIMATE_MIN_CONFIDENCE = 0.7
CLIMATE_MIN_INTERVAL_S = 30 * 60
CLIMATE_DEADBAND_C = 0.5


@dataclass(frozen=True)
class Preset:
    """How eager the autopilot is. Probabilities are thresholds on Jev's Noul."""

    name: str
    on_threshold: float
    off_threshold: float
    cooldown_s: int
    override_hold_s: int


PRESETS: dict[str, Preset] = {
    "conservative": Preset("conservative", 0.85, 0.15, 15 * 60, 60 * 60),
    "balanced": Preset("balanced", 0.75, 0.25, 5 * 60, 30 * 60),
    "aggressive": Preset("aggressive", 0.65, 0.35, 2 * 60, 15 * 60),
}
DEFAULT_PRESET = "balanced"


@dataclass(frozen=True)
class Levels:
    """User-editable level templates."""

    brightness: tuple[int, ...] = (10, 30, 50, 75, 100)
    color_temp: tuple[int, ...] = (2700, 3500, 4000)
    heating: tuple[str, ...] = ("off", "17", "20", "22")


@dataclass(frozen=True)
class EntitySnapshot:
    """What the engine needs to know about one controlled entity."""

    entity_id: str
    name: str
    state: str
    confirm: bool = False
    brightness_pct: float | None = None
    color_temp_k: int | None = None
    supports_brightness: bool = False
    supports_color_temp: bool = False
    min_color_temp_k: int | None = None
    max_color_temp_k: int | None = None
    target_temp: float | None = None
    hvac_modes: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True)
class Action:
    """A service call the engine wants made."""

    entity_id: str
    domain: str
    service: str
    data: Mapping[str, Any]
    reason: str
    confirm: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_id, self.service)


@dataclass
class History:
    """Per-room memory the engine reads. The controller owns and updates it."""

    last_change: dict[str, float] = field(default_factory=dict)
    last_override: dict[str, float] = field(default_factory=dict)
    blocked: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class Plan:
    """The questions for one room and how to read each answer back."""

    questions: dict[str, Question]
    index: dict[str, tuple[str, str]]  # question key -> (kind, entity_id)


@dataclass
class Decision:
    """Actions plus a per-entity trace of why."""

    actions: list[Action] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)


# --- level wording ---


def _brightness_words(pct: int) -> str:
    if pct <= 15:
        return "night light, barely lit, for moving around while others sleep"
    if pct <= 35:
        return "dim and relaxed, late evening"
    if pct <= 60:
        return "medium, comfortable for being in the room"
    if pct <= 85:
        return "bright, normal activity"
    return "full brightness, reading, cooking, cleaning or dark daytime"


def _color_temp_words(kelvin: int) -> str:
    if kelvin <= 3000:
        return "warm white, evening and winding down"
    if kelvin <= 3800:
        return "neutral white, ordinary daytime use"
    return "cool white, focused work in daytime"


def _heating_words(level: str) -> str:
    if level == "off":
        return "heating off: warm enough, windows open, or nobody home for hours"
    temp = float(level)
    if temp <= 18:
        return f"{level} °C eco: nobody in the room for a while, or people asleep"
    if temp <= 21:
        return f"{level} °C comfort: people at home and active"
    return f"{level} °C warm: cold outside and people resting in the room"


def parse_int_levels(text: str) -> tuple[int, ...]:
    """Parse `10, 30, 50` into sorted unique ints. Raises ValueError."""
    values = sorted(
        {int(part) for part in text.replace(";", ",").split(",") if part.strip()}
    )
    if len(values) < 2:
        raise ValueError("need at least two levels")
    return tuple(values)


def parse_heating_levels(text: str) -> tuple[str, ...]:
    """Parse `off, 17, 20, 22`. Raises ValueError."""
    out: list[str] = []
    for raw in text.replace(";", ",").split(","):
        part = raw.strip().lower()
        if not part:
            continue
        if part != "off":
            value = float(part)
            if not 5 <= value <= 35:
                raise ValueError("temperature out of range")
            part = f"{value:g}"
        if part not in out:
            out.append(part)
    if len(out) < 2:
        raise ValueError("need at least two levels")
    return tuple(out)


# --- questions ---


def build_plan(room: str, entities: Sequence[EntitySnapshot], levels: Levels) -> Plan:
    """One batch of atomic questions for a room. Jev never sees the keys."""
    questions: dict[str, Question] = {}
    index: dict[str, tuple[str, str]] = {}

    def add(kind: str, entity: EntitySnapshot, question: Question) -> None:
        key = f"{kind}_{len(questions)}"
        questions[key] = question
        index[key] = (kind, entity.entity_id)

    for entity in entities:
        if entity.state in UNUSABLE_STATES:
            continue
        label = f'"{entity.name}" in the {room}'
        domain = entity.domain
        if domain == "lock":
            add(
                "lock",
                entity,
                Noul(
                    f"Should the lock {label} be locked right now?",
                    true="Everyone has left, or it is night and people are in bed.",
                    false="People are coming and going, or someone is expected.",
                ),
            )
            continue
        if domain == "media_player":
            add(
                "off",
                entity,
                Noul(
                    f"Should the media player {label} be turned off now?",
                    true="Nobody is watching or listening, or everyone is asleep or away.",
                    false="Someone is likely watching or listening right now.",
                ),
            )
            continue
        if domain == "climate":
            add(
                "heat",
                entity,
                Choice(
                    f"Which heating level fits the {room} right now?",
                    {level: _heating_words(level) for level in levels.heating},
                ),
            )
            continue
        if domain == "light":
            add(
                "on",
                entity,
                Noul(
                    f"Should the light {label} be on right now?",
                    true="Someone is in or about to use the room and daylight is not enough.",
                    false="Nobody needs it, daylight is enough, or people are asleep.",
                ),
            )
            if entity.supports_brightness:
                add(
                    "bri",
                    entity,
                    Score(
                        f"If the light {label} is on, how bright should it be right now?",
                        [
                            f"{pct}%: {_brightness_words(pct)}"
                            for pct in levels.brightness
                        ],
                    ),
                )
            if entity.supports_color_temp:
                usable = _usable_color_temps(entity, levels.color_temp)
                if len(usable) >= 2:
                    add(
                        "ct",
                        entity,
                        Choice(
                            f"If the light {label} is on, which colour should it be?",
                            {f"{k}K": _color_temp_words(k) for k in usable},
                        ),
                    )
            continue
        add(
            "on",
            entity,
            Noul(
                f"Should {label} be on right now?",
                true="It serves a purpose for the people at home right now.",
                false="Nobody needs it right now.",
            ),
        )
    return Plan(questions, index)


def _usable_color_temps(entity: EntitySnapshot, levels: Sequence[int]) -> list[int]:
    low = entity.min_color_temp_k or 0
    high = entity.max_color_temp_k or 100_000
    return [k for k in levels if low <= k <= high]


# --- answers ---


def interpolate(score: float, levels: Sequence[int]) -> int:
    """Map Jev's continuous score (0..n-1) onto the level values."""
    top = len(levels) - 1
    score = min(max(score, 0.0), float(top))
    low = math.floor(score)
    if low >= top:
        return int(levels[top])
    frac = score - low
    return round(levels[low] + frac * (levels[low + 1] - levels[low]))


def decide(
    plan: Plan,
    entities: Sequence[EntitySnapshot],
    answers: Mapping[str, Answer],
    *,
    preset: Preset,
    levels: Levels,
    history: History,
    now: float,
    confirm_threshold: float,
) -> Decision:
    """Turn answers into actions under hysteresis, cooldown and override rules."""
    by_id = {e.entity_id: e for e in entities}
    grouped: dict[str, dict[str, Answer]] = {}
    for key, (kind, entity_id) in plan.index.items():
        answer = answers.get(key)
        if answer is None:
            continue
        # A choice outside the options we offered cannot be acted on; drop it.
        question = plan.questions.get(key)
        if isinstance(answer, ChoiceAnswer) and (
            not isinstance(question, Choice) or answer.choice not in question.criteria
        ):
            continue
        grouped.setdefault(entity_id, {})[kind] = answer

    decision = Decision()
    for entity_id, got in grouped.items():
        entity = by_id.get(entity_id)
        if entity is None or entity.state in UNUSABLE_STATES:
            continue
        trace: dict[str, Any] = {"entity_id": entity_id, "answers": _plain(got)}
        decision.trace.append(trace)

        since_override = now - history.last_override.get(entity_id, -math.inf)
        if since_override < preset.override_hold_s:
            trace["hold"] = f"manual override {int(since_override)} s ago"
            continue
        since_change = now - history.last_change.get(entity_id, -math.inf)
        if since_change < preset.cooldown_s:
            trace["hold"] = f"cooldown, changed {int(since_change)} s ago"
            continue

        if entity.domain == "climate":
            if since_change < CLIMATE_MIN_INTERVAL_S:
                trace["hold"] = "climate changed less than 30 min ago"
                continue
            action = _decide_climate(entity, got.get("heat"))
        elif entity.confirm or entity.domain == "lock":
            action = _decide_confirm(entity, got, confirm_threshold)
        elif entity.domain == "media_player":
            action = _decide_media(entity, got.get("off"), preset)
        elif entity.domain == "light":
            action = _decide_light(entity, got, preset, levels)
        else:
            action = _decide_toggle(entity, got.get("on"), preset)

        if action is None:
            trace.setdefault("hold", "no change needed")
            continue
        if action.key in history.blocked:
            trace["hold"] = "proposal blocked (pending or skipped)"
            continue
        trace["action"] = f"{action.domain}.{action.service}"
        trace["reason"] = action.reason
        decision.actions.append(action)
    return decision


def _plain(got: Mapping[str, Answer]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind, answer in got.items():
        if isinstance(answer, NoulAnswer):
            out[kind] = round(answer.noul, 3)
        elif isinstance(answer, ScoreAnswer):
            out[kind] = round(answer.score, 3)
        elif isinstance(answer, ChoiceAnswer):
            out[kind] = {
                "choice": answer.choice,
                "confidence": round(answer.confidence, 3),
            }
    return out


def _noul(answer: Answer | None) -> float | None:
    return answer.noul if isinstance(answer, NoulAnswer) else None


def _decide_toggle(
    entity: EntitySnapshot, answer: Answer | None, preset: Preset
) -> Action | None:
    p = _noul(answer)
    if p is None:
        return None
    if p >= preset.on_threshold and entity.state == "off":
        return Action(entity.entity_id, entity.domain, "turn_on", {}, f"p(on)={p:.2f}")
    if p <= preset.off_threshold and entity.state == "on":
        return Action(entity.entity_id, entity.domain, "turn_off", {}, f"p(on)={p:.2f}")
    return None


def _decide_media(
    entity: EntitySnapshot, answer: Answer | None, preset: Preset
) -> Action | None:
    p = _noul(answer)
    if p is None or p < preset.on_threshold or entity.state not in MEDIA_ACTIVE_STATES:
        return None
    return Action(entity.entity_id, "media_player", "turn_off", {}, f"p(off)={p:.2f}")


def _decide_confirm(
    entity: EntitySnapshot, got: Mapping[str, Answer], threshold: float
) -> Action | None:
    if entity.domain == "lock":
        p = _noul(got.get("lock"))
        # Unlocking is never proposed, whatever the model says.
        if p is not None and p >= threshold and entity.state == "unlocked":
            return Action(
                entity.entity_id, "lock", "lock", {}, f"p(lock)={p:.2f}", confirm=True
            )
        return None
    p = _noul(got.get("on"))
    if p is None:
        return None
    if p >= threshold and entity.state == "off":
        return Action(
            entity.entity_id, entity.domain, "turn_on", {}, f"p(on)={p:.2f}", confirm=True
        )
    if p <= 1 - threshold and entity.state == "on":
        return Action(
            entity.entity_id,
            entity.domain,
            "turn_off",
            {},
            f"p(on)={p:.2f}",
            confirm=True,
        )
    return None


def _decide_light(
    entity: EntitySnapshot,
    got: Mapping[str, Answer],
    preset: Preset,
    levels: Levels,
) -> Action | None:
    p = _noul(got.get("on"))
    if p is None:
        return None
    if p <= preset.off_threshold:
        if entity.state == "on":
            return Action(entity.entity_id, "light", "turn_off", {}, f"p(on)={p:.2f}")
        return None
    turn_on = p >= preset.on_threshold and entity.state == "off"
    if not turn_on and entity.state != "on":
        return None

    data: dict[str, Any] = {}
    reasons = [f"p(on)={p:.2f}"]
    bri = got.get("bri")
    if isinstance(bri, ScoreAnswer):
        target = interpolate(bri.score, levels.brightness)
        current = entity.brightness_pct
        if turn_on or current is None or abs(target - current) >= BRIGHTNESS_DEADBAND_PCT:
            data["brightness_pct"] = target
            reasons.append(f"brightness {target}%")
    ct = got.get("ct")
    if isinstance(ct, ChoiceAnswer) and ct.confidence >= COLOR_TEMP_MIN_CONFIDENCE:
        kelvin = int(ct.choice.rstrip("K"))
        current_k = entity.color_temp_k
        if (
            turn_on
            or current_k is None
            or abs(kelvin - current_k) >= COLOR_TEMP_DEADBAND_K
        ):
            data["color_temp_kelvin"] = kelvin
            reasons.append(f"colour {kelvin}K")
    if not turn_on and not data:
        return None
    return Action(entity.entity_id, "light", "turn_on", data, ", ".join(reasons))


def _decide_climate(entity: EntitySnapshot, answer: Answer | None) -> Action | None:
    if not isinstance(answer, ChoiceAnswer) or answer.confidence < CLIMATE_MIN_CONFIDENCE:
        return None
    reason = f"heating {answer.choice} (confidence {answer.confidence:.2f})"
    if answer.choice == "off":
        if entity.state == "off":
            return None
        return Action(
            entity.entity_id, "climate", "set_hvac_mode", {"hvac_mode": "off"}, reason
        )
    target = float(answer.choice)
    data: dict[str, Any] = {"temperature": target}
    if entity.state == "off":
        for mode in ("heat", "auto", "heat_cool"):
            if mode in entity.hvac_modes:
                data["hvac_mode"] = mode
                break
    elif (
        entity.target_temp is not None
        and abs(entity.target_temp - target) < CLIMATE_DEADBAND_C
    ):
        return None
    return Action(entity.entity_id, "climate", "set_temperature", data, reason)
