"""Engine rules, tested without Home Assistant."""

from __future__ import annotations

import pytest
from jevclient import Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer

from custom_components.jev_autopilot.engine import (
    PRESETS,
    EntitySnapshot,
    History,
    Levels,
    build_plan,
    decide,
    interpolate,
    parse_heating_levels,
    parse_int_levels,
)

BAL = PRESETS["balanced"]
LEVELS = Levels()
NOW = 1_000_000.0


def score(value: float, n: int = 5) -> ScoreAnswer:
    return ScoreAnswer(score=value, legend={}, probabilities={}, confidence=0.9)


def choice(value: str, confidence: float = 0.9) -> ChoiceAnswer:
    return ChoiceAnswer(choice=value, probabilities={}, confidence=confidence)


def run(entities, by_kind, history=None, preset=BAL, threshold=0.9):
    plan = build_plan("living room", entities, LEVELS)
    answers = {}
    for key, (kind, entity_id) in plan.index.items():
        if (kind, entity_id) in by_kind:
            answers[key] = by_kind[(kind, entity_id)]
    return decide(
        plan,
        entities,
        answers,
        preset=preset,
        levels=LEVELS,
        history=history or History(),
        now=NOW,
        confirm_threshold=threshold,
    )


LIGHT = EntitySnapshot(
    "light.a",
    "Ceiling",
    "off",
    supports_brightness=True,
    supports_color_temp=True,
    min_color_temp_k=2200,
    max_color_temp_k=4000,
)


def test_plan_builds_atomic_questions_without_ids():
    plan = build_plan("kitchen", [LIGHT], LEVELS)
    kinds = sorted(kind for kind, _ in plan.index.values())
    assert kinds == ["bri", "ct", "on"]
    for question in plan.questions.values():
        assert "light.a" not in question.instructions
        assert isinstance(question, Noul | Choice | Score)


def test_plan_skips_unavailable_and_out_of_range_colour():
    narrow = EntitySnapshot(
        "light.b",
        "B",
        "on",
        supports_color_temp=True,
        min_color_temp_k=3000,
        max_color_temp_k=3600,
    )
    gone = EntitySnapshot("switch.x", "X", "unavailable")
    plan = build_plan("hall", [narrow, gone], LEVELS)
    assert [k for k, _ in plan.index.values()] == ["on"]


def test_light_turns_on_with_level_and_colour():
    d = run(
        [LIGHT],
        {
            ("on", "light.a"): NoulAnswer(0.8),
            ("bri", "light.a"): score(2.5),
            ("ct", "light.a"): choice("2700K"),
        },
    )
    (action,) = d.actions
    assert action.service == "turn_on"
    assert action.data == {"brightness_pct": 62, "color_temp_kelvin": 2700}


def test_hysteresis_band_holds():
    on = EntitySnapshot("light.a", "A", "on", brightness_pct=50)
    assert run([on], {("on", "light.a"): NoulAnswer(0.5)}).actions == []
    assert run([LIGHT], {("on", "light.a"): NoulAnswer(0.5)}).actions == []


def test_light_turns_off_below_threshold():
    on = EntitySnapshot("light.a", "A", "on")
    (action,) = run([on], {("on", "light.a"): NoulAnswer(0.2)}).actions
    assert action.service == "turn_off"


def test_brightness_deadband():
    on = EntitySnapshot("light.a", "A", "on", brightness_pct=55, supports_brightness=True)
    small = run(
        [on], {("on", "light.a"): NoulAnswer(0.9), ("bri", "light.a"): score(2.0)}
    )
    assert small.actions == []
    big = run([on], {("on", "light.a"): NoulAnswer(0.9), ("bri", "light.a"): score(4.0)})
    assert big.actions[0].data == {"brightness_pct": 100}


def test_low_confidence_colour_ignored():
    d = run(
        [LIGHT],
        {
            ("on", "light.a"): NoulAnswer(0.9),
            ("ct", "light.a"): choice("4000K", confidence=0.4),
        },
    )
    assert "color_temp_kelvin" not in d.actions[0].data


def test_cooldown_and_override_hold():
    on = EntitySnapshot("light.a", "A", "on")
    ans = {("on", "light.a"): NoulAnswer(0.1)}
    h = History(last_change={"light.a": NOW - 60})
    assert run([on], ans, h).actions == []
    h = History(last_override={"light.a": NOW - 600})
    d = run([on], ans, h)
    assert d.actions == [] and "manual override" in d.trace[0]["hold"]
    h = History(last_override={"light.a": NOW - 3600})
    assert len(run([on], ans, h).actions) == 1


def test_preset_changes_threshold():
    ans = {("on", "light.a"): NoulAnswer(0.7)}
    assert run([LIGHT], ans).actions == []
    assert len(run([LIGHT], ans, preset=PRESETS["aggressive"]).actions) == 1


def test_switch_and_fan_toggle():
    sw = EntitySnapshot("switch.fan", "Fan plug", "off")
    fan = EntitySnapshot("fan.b", "Fan", "on")
    d = run(
        [sw, fan],
        {("on", "switch.fan"): NoulAnswer(0.9), ("on", "fan.b"): NoulAnswer(0.1)},
    )
    assert {(a.entity_id, a.service) for a in d.actions} == {
        ("switch.fan", "turn_on"),
        ("fan.b", "turn_off"),
    }


def test_media_player_only_off():
    tv = EntitySnapshot("media_player.tv", "TV", "playing")
    assert (
        run([tv], {("off", "media_player.tv"): NoulAnswer(0.9)}).actions[0].service
        == "turn_off"
    )
    tv_off = EntitySnapshot("media_player.tv", "TV", "off")
    assert run([tv_off], {("off", "media_player.tv"): NoulAnswer(0.0)}).actions == []


def test_lock_only_locks_and_needs_confirmation():
    lock = EntitySnapshot("lock.door", "Door", "unlocked")
    (action,) = run([lock], {("lock", "lock.door"): NoulAnswer(0.95)}).actions
    assert (action.service, action.confirm) == ("lock", True)
    locked = EntitySnapshot("lock.door", "Door", "locked")
    assert run([locked], {("lock", "lock.door"): NoulAnswer(0.0)}).actions == []
    assert run([lock], {("lock", "lock.door"): NoulAnswer(0.85)}).actions == []


def test_confirm_tier_switch_uses_confirm_threshold():
    cooker = EntitySnapshot("switch.cooker", "Cooker", "off", confirm=True)
    assert run([cooker], {("on", "switch.cooker"): NoulAnswer(0.85)}).actions == []
    (a,) = run([cooker], {("on", "switch.cooker"): NoulAnswer(0.95)}).actions
    assert a.confirm and a.service == "turn_on"
    on = EntitySnapshot("switch.cooker", "Cooker", "on", confirm=True)
    (a,) = run([on], {("on", "switch.cooker"): NoulAnswer(0.05)}).actions
    assert a.service == "turn_off"


def test_blocked_proposal_skipped():
    cooker = EntitySnapshot("switch.cooker", "Cooker", "off", confirm=True)
    h = History(blocked={("switch.cooker", "turn_on")})
    assert run([cooker], {("on", "switch.cooker"): NoulAnswer(0.99)}, h).actions == []


def test_climate_rules():
    trv = EntitySnapshot(
        "climate.t", "TRV", "heat", target_temp=20.0, hvac_modes=("off", "heat")
    )
    assert run([trv], {("heat", "climate.t"): choice("20")}).actions == []
    assert run([trv], {("heat", "climate.t"): choice("22", 0.5)}).actions == []
    assert run([trv], {("heat", "climate.t"): choice("22", float("nan"))}).actions == []
    (a,) = run([trv], {("heat", "climate.t"): choice("17")}).actions
    assert a.service == "set_temperature" and a.data == {"temperature": 17.0}
    (a,) = run([trv], {("heat", "climate.t"): choice("off")}).actions
    assert a.data == {"hvac_mode": "off"}
    h = History(last_change={"climate.t": NOW - 20 * 60})
    assert run([trv], {("heat", "climate.t"): choice("17")}, h).actions == []
    off = EntitySnapshot("climate.t", "TRV", "off", hvac_modes=("off", "heat"))
    (a,) = run([off], {("heat", "climate.t"): choice("20")}).actions
    assert a.data == {"temperature": 20.0, "hvac_mode": "heat"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 10), (4, 100), (1.5, 40), (-1, 10), (9, 100)],
)
def test_interpolate(value, expected):
    assert interpolate(value, (10, 30, 50, 75, 100)) == expected


def test_level_parsers():
    assert parse_int_levels("50, 10;30") == (10, 30, 50)
    assert parse_heating_levels("off, 17, 20.5") == ("off", "17", "20.5")
    for bad in ("10", "a,b"):
        with pytest.raises(ValueError):
            parse_int_levels(bad)
    with pytest.raises(ValueError):
        parse_heating_levels("off, 50")


def test_choice_outside_the_offered_options_is_ignored():
    d = run(
        [LIGHT],
        {("on", "light.a"): NoulAnswer(0.9), ("ct", "light.a"): choice("warm")},
    )
    assert "color_temp_kelvin" not in d.actions[0].data
    trv = EntitySnapshot("climate.t", "TRV", "heat", hvac_modes=("off", "heat"))
    assert run([trv], {("heat", "climate.t"): choice("cosy")}).actions == []
