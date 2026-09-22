"""The decide-act loop against a real hass core and a fake Jev."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, Event, HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir
from jevclient import JevAuthError, JevConnectionError, NoulAnswer
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.jev_autopilot.const import CONF_BUDGET, CONF_PRESET, DOMAIN

from .conftest import DEFAULT_ROOM, make_entry, room


@pytest.fixture
async def calls(hass: HomeAssistant) -> dict[str, list[ServiceCall]]:
    hass.states.async_set("light.ceiling", "off", {"supported_color_modes": ["onoff"]})
    hass.states.async_set("lock.front", "unlocked")
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("automation.old_lights", "on")
    light: list[ServiceCall] = []

    async def light_service(call: ServiceCall) -> None:
        light.append(call)
        state = "on" if call.service == "turn_on" else "off"
        for entity_id in (
            call.data["entity_id"]
            if isinstance(call.data["entity_id"], list)
            else [call.data["entity_id"]]
        ):
            hass.states.async_set(
                entity_id,
                state,
                {"supported_color_modes": ["onoff"]},
                context=call.context,
            )

    hass.services.async_register("light", "turn_on", light_service)
    hass.services.async_register("light", "turn_off", light_service)
    return {
        "light": light,
        "lock": async_mock_service(hass, "lock", "lock"),
        "notify": async_mock_service(hass, "notify", "phone"),
        "auto_off": async_mock_service(hass, "automation", "turn_off"),
        "auto_on": async_mock_service(hass, "automation", "turn_on"),
    }


async def setup(hass: HomeAssistant, **options: Any):
    entry = make_entry(room(**DEFAULT_ROOM), **options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (controller,) = entry.runtime_data.rooms.values()
    return entry, controller


async def test_setup_takes_over_and_creates_entities(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    assert controller.enabled
    assert [c.data["entity_id"] for c in calls["auto_off"]] == ["automation.old_lights"]
    assert hass.states.get("switch.living_room_autopilot").state == "on"
    assert hass.states.get("select.living_room_preset").state == "balanced"
    assert hass.states.get("sensor.living_room_status").state == "ok"
    assert hass.states.get("sensor.jev_autopilot_calls_today").state == "0"


async def test_run_acts_directly_and_proposes_lock(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_run()
    await hass.async_block_till_done()
    assert [c.service for c in calls["light"]] == ["turn_on"]
    assert calls["lock"] == []
    (note,) = calls["notify"]
    actions = [a["action"] for a in note.data["data"]["actions"]]
    assert actions[0].startswith("JEVAP_RUN_") and actions[1].startswith("JEVAP_SKIP_")
    state, _ = jev.calls[-1]
    assert "light.ceiling" not in state and "lock.front" not in state
    decision = hass.states.get("sensor.living_room_last_decision")
    assert "light.turn_on light.ceiling" in decision.state
    assert hass.states.get("sensor.jev_autopilot_calls_today").state == "1"
    # Our own change is not a manual override.
    assert hass.states.get("sensor.living_room_manual_overrides_today").state == "0"


async def test_confirm_run_executes(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_run()
    await hass.async_block_till_done()
    run = calls["notify"][0].data["data"]["actions"][0]["action"]
    hass.bus.async_fire("mobile_app_notification_action", {"action": run})
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["lock"]] == ["lock.front"]
    # A second tap on the same token does nothing.
    hass.bus.async_fire("mobile_app_notification_action", {"action": run})
    await hass.async_block_till_done()
    assert len(calls["lock"]) == 1


async def test_confirm_skip_suppresses(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_run()
    await hass.async_block_till_done()
    skip = calls["notify"][0].data["data"]["actions"][1]["action"]
    hass.bus.async_fire("mobile_app_notification_action", {"action": skip})
    await hass.async_block_till_done()
    sent = len(calls["notify"])
    await controller.async_run()
    await hass.async_block_till_done()
    assert calls["lock"] == []
    assert len(calls["notify"]) == sent  # no new proposal while suppressed
    assert any("vetoed" in row for row in entry.runtime_data.log)


async def test_pending_proposal_is_not_repeated(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_run()
    await controller.async_run()
    await hass.async_block_till_done()
    assert len(calls["notify"]) == 1


async def test_manual_override_holds(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    jev.policy["on"] = NoulAnswer(0.02)
    hass.states.async_set(
        "light.ceiling", "on", {"supported_color_modes": ["onoff"]}, context=Context()
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_manual_overrides_today").state == "1"
    await controller.async_run()
    await hass.async_block_till_done()
    assert calls["light"] == []


async def test_degrade_and_recover(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    jev.error = JevConnectionError("down")
    for _ in range(3):
        await controller.async_run()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "degraded"
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]
    registry = ir.async_get(hass)
    issue_id = f"degraded_{controller.subentry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert any("Jev unreachable" in c.data["message"] for c in calls["notify"])
    # Each failed run retried once.
    assert len(jev.calls) == 6

    jev.error = None
    await controller.async_run()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "ok"
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert len(calls["auto_off"]) == 2


async def test_auth_error_starts_reauth(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    jev.error = JevAuthError("revoked")
    await controller.async_run()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    before = len(jev.calls)
    await controller.async_run()
    assert len(jev.calls) == before  # no more calls until the key is fixed
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]
    assert hass.states.get("sensor.living_room_status").state == "degraded"


async def test_budget_pauses(hass, jev, calls) -> None:
    entry, controller = await setup(hass, **{CONF_BUDGET: 1})
    await controller.async_run()
    await controller.async_run()
    await hass.async_block_till_done()
    assert len(jev.calls) == 1
    assert hass.states.get("sensor.living_room_status").state == "paused"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "budget_reached") is not None
    # Paused rooms hand the house back to its automations...
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]
    # ...and take it again once a new day resets the count.
    takes = len(calls["auto_off"])
    runtime = entry.runtime_data
    runtime._day = None
    await controller.async_run()
    await hass.async_block_till_done()
    assert len(calls["auto_off"]) == takes + 1
    assert hass.states.get("sensor.living_room_status").state == "ok"


async def test_switch_off_hands_back(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.living_room_autopilot"}, blocking=True
    )
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]
    await controller.async_run()
    assert jev.calls == []
    assert hass.states.get("sensor.living_room_status").state == "disabled"


async def test_preset_select(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.living_room_preset", "option": "aggressive"},
        blocking=True,
    )
    assert controller.preset.name == "aggressive"


async def test_unload_releases_automations(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]
    assert runtime.taken == {}


async def test_removing_room_removes_its_device(hass, jev, calls) -> None:
    from homeassistant.helpers import device_registry as dr

    entry, controller = await setup(hass)
    registry = dr.async_get(hass)
    ident = (DOMAIN, controller.subentry_id)
    assert registry.async_get_device_by_identifier(ident, entry.entry_id) is not None
    hass.config_entries.async_remove_subentry(entry, controller.subentry_id)
    await hass.async_block_till_done()
    assert registry.async_get_device_by_identifier(ident, entry.entry_id) is None
    assert entry.state is ConfigEntryState.LOADED
    assert calls["auto_on"], "automation handed back when its room goes"


async def test_reenabling_a_paused_room_keeps_automations_released(
    hass, jev, calls
) -> None:
    entry, controller = await setup(hass, **{CONF_BUDGET: 1})
    await controller.async_run()
    await controller.async_run()
    await hass.async_block_till_done()
    assert controller.paused
    takes = len(calls["auto_off"])
    for service in ("turn_off", "turn_on"):
        await hass.services.async_call(
            "switch",
            service,
            {"entity_id": "switch.living_room_autopilot"},
            blocking=True,
        )
    assert len(calls["auto_off"]) == takes
    assert entry.runtime_data.taken == {}


async def test_reenabling_after_auth_failure_keeps_automations_released(
    hass, jev, calls
) -> None:
    entry, controller = await setup(hass)
    jev.error = JevAuthError("revoked")
    await controller.async_run()
    await hass.async_block_till_done()
    takes = len(calls["auto_off"])
    for service in ("turn_off", "turn_on"):
        await hass.services.async_call(
            "switch",
            service,
            {"entity_id": "switch.living_room_autopilot"},
            blocking=True,
        )
    assert len(calls["auto_off"]) == takes
    assert entry.runtime_data.taken == {}


async def test_run_in_flight_at_unload_does_nothing(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    jev.policy["on"] = NoulAnswer(0.95)
    gate = asyncio.Event()
    answer = jev.ask.side_effect

    async def slow(*args: Any) -> Any:
        await gate.wait()
        return await answer(*args)

    jev.ask.side_effect = slow
    run = hass.async_create_task(controller.async_run())
    await asyncio.sleep(0)
    assert await hass.config_entries.async_unload(entry.entry_id)
    offs = len(calls["auto_off"])
    gate.set()
    with contextlib.suppress(asyncio.CancelledError):
        await run
    await hass.async_block_till_done()
    assert run.cancelled()
    assert calls["light"] == []
    assert len(calls["auto_off"]) == offs


async def test_missing_automation_is_kept_until_it_exists(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    runtime = entry.runtime_data
    hass.states.async_remove("automation.old_lights")
    await runtime.async_release(controller.subentry_id)
    assert calls["auto_on"] == []
    assert runtime.taken == {controller.subentry_id: ["automation.old_lights"]}


async def test_orphaned_automations_are_released(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    runtime = entry.runtime_data
    runtime.taken["gone-room"] = ["automation.old_lights"]
    await runtime.async_release_orphans()
    assert "gone-room" not in runtime.taken
    assert controller.subentry_id in runtime.taken
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]


async def test_attribute_change_counts_as_override(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    hass.states.async_set(
        "light.ceiling",
        "off",
        {"supported_color_modes": ["onoff"], "color_temp_kelvin": 2700},
        context=Context(),
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_manual_overrides_today").state == "1"


async def test_default_preset_applies_until_room_picks_one(hass, jev, calls) -> None:
    entry, controller = await setup(hass, **{CONF_PRESET: "conservative"})
    assert controller.preset.name == "conservative"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.living_room_preset", "option": "aggressive"},
        blocking=True,
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    (controller,) = entry.runtime_data.rooms.values()
    assert controller.preset.name == "aggressive"
    assert hass.states.get("select.living_room_preset").state == "aggressive"


async def test_run_stopped_while_acting_stops_acting(hass, jev, calls) -> None:
    hass.states.async_set("light.second", "off", {"supported_color_modes": ["onoff"]})
    entry = make_entry(
        room(**{**DEFAULT_ROOM, "lights": ["light.ceiling", "light.second"]})
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (controller,) = entry.runtime_data.rooms.values()
    gate = asyncio.Event()
    real = controller.async_execute

    async def slow(action: Any, **kwargs: Any) -> bool:
        done = await real(action, **kwargs)
        await gate.wait()
        return done

    controller.async_execute = slow  # type: ignore[method-assign]
    run = hass.async_create_task(controller.async_run())
    for _ in range(20):
        await asyncio.sleep(0)
        if calls["light"]:
            break
    assert len(calls["light"]) == 1
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert run.done()
    assert len(calls["light"]) == 1
    assert runtime.taken == {}


async def test_take_waits_until_started(hass, jev, calls) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    hass.set_state(CoreState.starting)
    entry, controller = await setup(hass)
    assert calls["auto_off"] == []
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["auto_off"]] == ["automation.old_lights"]


async def test_spent_budget_pauses_every_room(hass, jev, calls) -> None:
    hass.states.async_set("light.other", "off", {"supported_color_modes": ["onoff"]})
    hass.states.async_set("automation.other", "on")
    entry = make_entry(
        room(**DEFAULT_ROOM),
        room("Kitchen", lights=["light.other"], yield_automations=["automation.other"]),
        **{CONF_BUDGET: 1},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first, second = entry.runtime_data.rooms.values()
    await first.async_run()
    await first.async_run()
    await hass.async_block_till_done()
    assert first.paused and second.paused
    assert entry.runtime_data.taken == {}


async def test_removing_entry_releases_and_deletes_store(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    hass.states.async_remove("automation.old_lights")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("automation.old_lights", "off")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]


async def test_questions_carry_no_resident_names(hass, jev, calls) -> None:
    hass.states.async_set("person.alice", "home", {"friendly_name": "Alice"})
    hass.states.async_set("person.zhang", "home", {"friendly_name": "张三"})
    hass.states.async_set(
        "light.ceiling",
        "off",
        {"supported_color_modes": ["onoff"], "friendly_name": "张三的台灯"},
    )
    hass.states.async_set(
        "lock.front", "unlocked", {"friendly_name": "Alice's front door"}
    )
    entry = make_entry(room("alice room 198.51.100.9", **DEFAULT_ROOM))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (controller,) = entry.runtime_data.rooms.values()
    await controller.async_run()
    await hass.async_block_till_done()
    (state, questions), *_ = jev.calls[1:] or jev.calls
    sent = state + repr(questions)
    for leak in ("Alice", "alice", "张三", "198.51.100.9"):
        assert leak not in sent
    assert "a resident's front door" in sent
    # The phone is the household's own, so it names the lock as they do.
    (note,) = calls["notify"]
    assert "Alice's front door" in note.data["message"]


def _hold_turn_off(hass: HomeAssistant, gate: asyncio.Event) -> asyncio.Event:
    held = asyncio.Event()

    async def turn_off(call: ServiceCall) -> None:
        held.set()
        await gate.wait()

    hass.services.async_register("automation", "turn_off", turn_off)
    return held


async def test_switch_off_during_take_still_hands_back(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_set_enabled(False)
    ons = len(calls["auto_on"])
    gate = asyncio.Event()
    held = _hold_turn_off(hass, gate)
    take = hass.async_create_task(controller.async_set_enabled(True))
    await asyncio.wait_for(held.wait(), 5)
    off = hass.async_create_task(controller.async_set_enabled(False))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(take, off), 5)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["auto_on"][ons:]] == [
        "automation.old_lights"
    ]
    assert entry.runtime_data.taken.get(controller.subentry_id, []) == []


async def test_unload_during_take_still_hands_back(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_set_enabled(False)
    ons = len(calls["auto_on"])
    gate = asyncio.Event()
    held = _hold_turn_off(hass, gate)
    take = hass.async_create_task(controller.async_set_enabled(True))
    await asyncio.wait_for(held.wait(), 5)
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(take, unload), 5)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["auto_on"][ons:]] == [
        "automation.old_lights"
    ]


async def test_failed_turn_off_is_not_recorded(hass, jev, calls) -> None:
    from homeassistant.exceptions import HomeAssistantError

    entry, controller = await setup(hass)
    await controller.async_set_enabled(False)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("nope")

    hass.services.async_register("automation", "turn_off", broken)
    await controller.async_set_enabled(True)
    assert entry.runtime_data.taken.get(controller.subentry_id, []) == []


async def test_confirmation_after_unload_does_nothing(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_run()
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    (token,) = runtime.pending
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # The listener is gone after unload; call the handler as a late tap would.
    await runtime.async_handle_action(Event("x", {"action": f"JEVAP_RUN_{token}"}))
    assert calls["lock"] == []


async def test_release_cancelled_halfway_keeps_the_rest(hass, jev, calls) -> None:
    hass.states.async_set("automation.second", "on")
    entry, controller = await setup(hass)
    runtime = entry.runtime_data
    runtime.taken[controller.subentry_id] = ["automation.old_lights", "automation.second"]
    gate = asyncio.Event()
    held = asyncio.Event()

    async def turn_on(call: ServiceCall) -> None:
        held.set()
        await gate.wait()

    hass.services.async_register("automation", "turn_on", turn_on)
    release = hass.async_create_task(runtime.async_release(controller.subentry_id))
    await asyncio.wait_for(held.wait(), 5)
    release.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await release
    gate.set()
    await hass.async_block_till_done()
    assert runtime.taken[controller.subentry_id] == [
        "automation.old_lights",
        "automation.second",
    ]


async def test_removal_that_cannot_hand_back_tells_the_user(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    hass.states.async_remove("automation.old_lights")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    notes = persistent_notification._async_get_or_create_notifications(hass)
    (note,) = [n for n in notes.values() if "automation.old_lights" in n["message"]]
    assert "still off" in note["title"]


async def test_enable_after_unload_does_nothing(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    offs = len(calls["auto_off"])
    await controller.async_set_enabled(True)
    assert len(calls["auto_off"]) == offs


async def test_malformed_reply_counts_as_failure(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    jev.error = ValueError("Retry-After: Wed, 21 Oct 2026 07:28:00 GMT")
    for _ in range(3):
        await controller.async_run()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "degraded"
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]


async def test_run_tap_after_switch_off_does_nothing(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    await controller.async_run()
    await hass.async_block_till_done()
    run = calls["notify"][0].data["data"]["actions"][0]["action"]
    await controller.async_set_enabled(False)
    hass.bus.async_fire("mobile_app_notification_action", {"action": run})
    await hass.async_block_till_done()
    assert calls["lock"] == []


async def test_takeover_is_on_disk_before_turn_off(
    hass, jev, calls, hass_storage
) -> None:
    entry, controller = await setup(hass)
    await controller.async_set_enabled(False)
    await hass.async_block_till_done()
    gate = asyncio.Event()
    held = _hold_turn_off(hass, gate)
    take = hass.async_create_task(controller.async_set_enabled(True))
    await asyncio.wait_for(held.wait(), 5)
    try:
        (stored,) = [v for k, v in hass_storage.items() if k.startswith(DOMAIN)]
        assert stored["data"]["taken"] == {
            controller.subentry_id: ["automation.old_lights"]
        }
    finally:
        gate.set()
        await asyncio.wait_for(take, 5)
        await hass.async_block_till_done()


async def test_failure_after_the_reply_still_degrades(hass, jev, calls) -> None:
    entry, controller = await setup(hass)
    with patch(
        "custom_components.jev_autopilot.controller.decide",
        side_effect=ValueError("invalid literal for int()"),
    ):
        for _ in range(3):
            await controller.async_run()
        await hass.async_block_till_done()
    assert controller.failures == 3
    assert hass.states.get("sensor.living_room_status").state == "degraded"
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]


async def test_invalid_service_data_is_a_failed_call_not_a_crash(
    hass, jev, calls
) -> None:
    async def reject(call: ServiceCall) -> None:
        raise vol.Invalid("bad data")

    hass.services.async_register("light", "turn_on", reject)
    entry, controller = await setup(hass)
    await controller.async_run()
    await hass.async_block_till_done()
    assert controller.failures == 0
    assert hass.states.get("sensor.living_room_status").state == "ok"


async def test_stop_hands_back_and_persists(
    hass, jev, calls, hass_storage, caplog
) -> None:
    entry, controller = await setup(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["auto_on"]] == ["automation.old_lights"]
    (stored,) = [v for k, v in hass_storage.items() if k.startswith(DOMAIN)]
    assert not any(stored["data"]["taken"].values())
    # Unloading afterwards does not hand back a second time.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(calls["auto_on"]) == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_auth_error_hands_back_every_room(hass, jev, calls) -> None:
    hass.states.async_set("automation.bedroom", "on")
    entry = make_entry(
        room(**DEFAULT_ROOM),
        room(
            "Bedroom", lights=["light.ceiling"], yield_automations=["automation.bedroom"]
        ),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    living, bedroom = entry.runtime_data.rooms.values()
    jev.error = JevAuthError("revoked")
    await living.async_run()
    await hass.async_block_till_done()
    assert sorted(c.data["entity_id"] for c in calls["auto_on"]) == [
        "automation.bedroom",
        "automation.old_lights",
    ]
    assert bedroom.auth_failed
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
