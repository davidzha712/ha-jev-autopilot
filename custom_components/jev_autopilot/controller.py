"""One room: triggers, the Jev call, execution, override detection, takeover."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_NAME
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util
from jevclient import JevAuthError, JevError, JevValidationError

from .const import (
    CONF_BRIGHTNESS_LEVELS,
    CONF_CLIMATES,
    CONF_COLOR_TEMP_LEVELS,
    CONF_CONFIRM_ENTITIES,
    CONF_CONFIRM_THRESHOLD,
    CONF_CONTEXT,
    CONF_FANS,
    CONF_HEATING_LEVELS,
    CONF_HOUSE_NOTES,
    CONF_LIGHTS,
    CONF_MEDIA,
    CONF_PATROL,
    CONF_PRESET,
    CONF_ROOM_NOTES,
    CONF_SWITCHES,
    CONF_YIELD,
    DEBOUNCE_S,
    DEFAULT_BRIGHTNESS_LEVELS,
    DEFAULT_COLOR_TEMP_LEVELS,
    DEFAULT_CONFIRM_THRESHOLD,
    DEFAULT_HEATING_LEVELS,
    DEFAULT_PATROL,
    DOMAIN,
    FAILURES_TO_DEGRADE,
    MIN_CONFIRM_THRESHOLD,
    MIN_PATROL,
)
from .engine import (
    DEFAULT_PRESET,
    PRESETS,
    Action,
    History,
    Levels,
    Preset,
    build_plan,
    decide,
    parse_heating_levels,
    parse_int_levels,
)
from .state import build_state, resident_names, scrub, snapshot

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry

    from .runtime import Runtime

_LOGGER = logging.getLogger(__package__)

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_DEGRADED = "degraded"
STATUS_PAUSED = "paused"
STATUSES = [STATUS_OK, STATUS_DISABLED, STATUS_DEGRADED, STATUS_PAUSED]

# What a person can change on a device. A foreign change to any of these is an override.
_OVERRIDE_ATTRS = ("brightness", "color_temp_kelvin", "temperature", "percentage")


def levels_from_options(options: dict[str, Any]) -> Levels:
    return Levels(
        brightness=parse_int_levels(
            options.get(CONF_BRIGHTNESS_LEVELS, DEFAULT_BRIGHTNESS_LEVELS)
        ),
        color_temp=parse_int_levels(
            options.get(CONF_COLOR_TEMP_LEVELS, DEFAULT_COLOR_TEMP_LEVELS)
        ),
        heating=parse_heating_levels(
            options.get(CONF_HEATING_LEVELS, DEFAULT_HEATING_LEVELS)
        ),
    )


class RoomController:
    """Runs the decide-act loop for one room subentry."""

    def __init__(
        self, hass: HomeAssistant, runtime: Runtime, subentry: ConfigSubentry
    ) -> None:
        self.hass = hass
        self.runtime = runtime
        self.subentry_id = subentry.subentry_id
        self.data = dict(subentry.data)
        self.name: str = self.data.get(CONF_NAME) or subentry.title
        options = dict(runtime.entry.options)
        self.levels = levels_from_options(options)
        self.preset_name: str = runtime.presets.get(
            self.subentry_id, options.get(CONF_PRESET, DEFAULT_PRESET)
        )
        self.confirm_threshold = max(
            float(options.get(CONF_CONFIRM_THRESHOLD, DEFAULT_CONFIRM_THRESHOLD)),
            MIN_CONFIRM_THRESHOLD,
        )
        self.patrol = timedelta(
            minutes=max(int(options.get(CONF_PATROL, DEFAULT_PATROL)), MIN_PATROL)
        )
        self.house_notes: str = options.get(CONF_HOUSE_NOTES, "") or ""
        self.enabled = False
        self.degraded = False
        self.paused = False
        self.auth_failed = False
        self._stopped = False
        # Runs in flight, so stop can cancel them; take and release never interleave.
        self._runs: set[asyncio.Task[Any]] = set()
        self._automations_lock = asyncio.Lock()
        self.failures = 0
        self.history = History()
        self.last: dict[str, Any] = {}
        self.overrides_today = 0
        self._override_day = dt_util.now().date()
        self._ours: deque[str] = deque(maxlen=256)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=DEBOUNCE_S, immediate=False, function=self.async_run
        )

    # --- configuration views ---

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_{self.subentry_id}"

    @property
    def preset(self) -> Preset:
        return PRESETS.get(self.preset_name, PRESETS[DEFAULT_PRESET])

    @property
    def confirm_entities(self) -> list[str]:
        return list(self.data.get(CONF_CONFIRM_ENTITIES, []))

    @property
    def direct_entities(self) -> list[str]:
        return [
            e
            for key in (CONF_LIGHTS, CONF_SWITCHES, CONF_FANS, CONF_CLIMATES, CONF_MEDIA)
            for e in self.data.get(key, [])
        ]

    @property
    def controlled(self) -> list[str]:
        return self.direct_entities + self.confirm_entities

    @property
    def stopped(self) -> bool:
        """True once the room has been unloaded or removed."""
        return self._stopped

    @property
    def in_control(self) -> bool:
        """True while this room, not your automations, should be running it."""
        return (
            self.enabled
            and not self._stopped
            and not (self.degraded or self.paused or self.auth_failed)
        )

    @property
    def status(self) -> str:
        if not self.enabled:
            return STATUS_DISABLED
        if self.degraded or self.auth_failed:
            return STATUS_DEGRADED
        if self.paused:
            return STATUS_PAUSED
        return STATUS_OK

    # --- lifecycle ---

    @callback
    def async_start(self) -> None:
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, self.controlled, self._on_controlled
            )
        )
        context = list(self.data.get(CONF_CONTEXT, []))
        if context:
            self._unsubs.append(
                async_track_state_change_event(self.hass, context, self._on_context)
            )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_patrol, self.patrol)
        )

    async def async_stop(self, *, release: bool) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._debouncer.async_shutdown()
        current = asyncio.current_task()
        runs = [task for task in self._runs if task is not current]
        for task in runs:
            task.cancel()
        for task in runs:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        if release:
            await self._release_automations()

    async def async_set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if enabled:
            await self._take_automations()
            await self._debouncer.async_call()
        else:
            await self._release_automations()
        self._notify_entities()

    def set_preset(self, name: str) -> None:
        """A preset picked for this room; it outlives the global default."""
        if name in PRESETS:
            self.preset_name = name
            self.runtime.presets[self.subentry_id] = name
            self.runtime.async_save()
            self._notify_entities()

    # --- triggers ---

    @callback
    def _on_context(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or old.state == new.state:
            return
        self._background(self._debouncer.async_call(), "debounce")

    @callback
    def _on_patrol(self, _now: Any) -> None:
        self._background(self.async_run(), "patrol")

    @callback
    def _background(self, coro: Any, what: str) -> None:
        # Tied to the entry so unloading cancels a run still waiting on Jev.
        self.runtime.entry.async_create_background_task(
            self.hass, coro, f"{DOMAIN} {what} {self.subentry_id}"
        )

    @callback
    def _on_controlled(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None:
            return
        if "unavailable" in (old.state, new.state) or "unknown" in (old.state, new.state):
            return
        ctx = new.context
        if ctx.id in self._ours or (ctx.parent_id and ctx.parent_id in self._ours):
            return
        changed = old.state != new.state or any(
            old.attributes.get(a) != new.attributes.get(a) for a in _OVERRIDE_ATTRS
        )
        if not changed:
            return
        self.history.last_override[new.entity_id] = time.time()
        today = dt_util.now().date()
        if today != self._override_day:
            self._override_day, self.overrides_today = today, 0
        self.overrides_today += 1
        self._notify_entities()

    # --- the loop ---

    async def async_run(self) -> None:
        """Ask Jev about the room and act on the answer."""
        if not self.enabled or self.auth_failed or self._stopped:
            return
        task = asyncio.current_task()
        if task is None:
            await self._run()
            return
        self._runs.add(task)
        try:
            await self._run()
        finally:
            self._runs.discard(task)

    async def async_pause(self) -> None:
        """The shared budget is spent: hand this room back to its automations."""
        if not self.paused:
            self.paused = True
            self._notify_entities()
        await self._release_automations()

    async def _run(self) -> None:
        # A room that cannot ask must not leave the house without its automations.
        # The budget is shared, so every room pauses, not only the one that noticed.
        if not self.runtime.budget_left():
            for room in list(self.runtime.rooms.values()):
                await room.async_pause()
            return
        if self.paused:
            self.paused = False
            self._notify_entities()
            await self._take_automations()
        confirm = set(self.confirm_entities)
        snapshots = [
            snap
            for entity_id in self.controlled
            if (snap := snapshot(self.hass, entity_id, confirm=entity_id in confirm))
        ]
        # Question text leaves the house too, so names are scrubbed there as well.
        # The unscrubbed snapshots stay for deciding and for the phone notification.
        residents = resident_names(self.hass)
        plan = build_plan(
            scrub(self.name, residents),
            [
                dataclasses.replace(snap, name=scrub(snap.name, residents))
                for snap in snapshots
            ],
            self.levels,
        )
        if not plan.questions:
            return
        state = build_state(
            self.hass,
            room=self.name,
            controlled=self.controlled,
            context=list(self.data.get(CONF_CONTEXT, [])),
            overrides=self.history.last_override,
            house_notes=self.house_notes,
            room_notes=self.data.get(CONF_ROOM_NOTES, "") or "",
        )
        try:
            response = await self.runtime.ask(state, plan.questions)
        except JevAuthError:
            if self._stopped:
                return
            self.auth_failed = True
            await self._release_automations()
            self._notify_entities()
            self.runtime.entry.async_start_reauth(self.hass)
            return
        except JevValidationError as err:
            _LOGGER.error(
                "Jev rejected the questions for %s (%s); keys %s",
                self.name,
                err,
                sorted(plan.questions),
            )
            await self._failed(str(err))
            return
        except JevError as err:
            await self._failed(str(err))
            return
        # The room may have been switched off or unloaded while Jev was thinking.
        if not self.enabled or self._stopped:
            return
        await self._succeeded()
        # A success clears degraded; a pause or auth failure meanwhile still holds.
        if not self.in_control:
            return

        now = time.time()
        self.history.blocked = self.runtime.blocked_keys(now)
        decision = decide(
            plan,
            snapshots,
            response.answers,
            preset=self.preset,
            levels=self.levels,
            history=self.history,
            now=now,
            confirm_threshold=self.confirm_threshold,
        )
        names = {s.entity_id: s.name for s in snapshots}
        done: list[str] = []
        for action in decision.actions:
            if self._stopped or not self.enabled:
                return
            if action.confirm:
                if await self.runtime.propose(self, action, names[action.entity_id]):
                    done.append(
                        f"asked: {action.domain}.{action.service} {action.entity_id}"
                    )
                else:
                    _LOGGER.warning(
                        "No notify service configured; cannot confirm %s",
                        action.entity_id,
                    )
            elif await self.async_execute(action):
                done.append(f"{action.domain}.{action.service} {action.entity_id}")

        if self._stopped:
            return
        self.last = {
            "at": dt_util.utcnow().isoformat(),
            "summary": "; ".join(done) if done else "hold",
            "actions": done,
            "trace": decision.trace,
            "model": response.model,
            "latency_ms": round(response.latency_ms),
            "input_tokens": response.usage.input_tokens,
            "preset": self.preset_name,
        }
        self.runtime.record({"room": self.name, **self.last})
        self._notify_entities()

    async def async_execute(self, action: Action, *, confirmed: bool = False) -> bool:
        """Make the service call under a context this room can recognise later."""
        context = Context()
        self._ours.append(context.id)
        try:
            await self.hass.services.async_call(
                action.domain,
                action.service,
                {"entity_id": action.entity_id, **action.data},
                blocking=True,
                context=context,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning(
                "%s.%s on %s failed: %s",
                action.domain,
                action.service,
                action.entity_id,
                err,
            )
            return False
        self.history.last_change[action.entity_id] = time.time()
        if confirmed:
            self.runtime.record(
                {
                    "room": self.name,
                    "at": dt_util.utcnow().isoformat(),
                    "confirmed": f"{action.domain}.{action.service} {action.entity_id}",
                }
            )
        return True

    def record_veto(self, action: Action) -> None:
        self.runtime.record(
            {
                "room": self.name,
                "at": dt_util.utcnow().isoformat(),
                "vetoed": f"{action.domain}.{action.service} {action.entity_id}",
            }
        )

    # --- failure handling ---

    @property
    def _issue_id(self) -> str:
        return f"degraded_{self.subentry_id}"

    async def _failed(self, error: str) -> None:
        if self._stopped:
            return
        self.failures += 1
        # Warn until the room degrades, then stay quiet: a patrol every few minutes
        # would otherwise repeat the same line all night.
        log = _LOGGER.debug if self.degraded else _LOGGER.warning
        log("Jev call for %s failed (%s in a row): %s", self.name, self.failures, error)
        if self.failures >= FAILURES_TO_DEGRADE and not self.degraded:
            self.degraded = True
            await self._release_automations()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="room_degraded",
                translation_placeholders={"room": self.name, "error": error[:200]},
            )
            await self.runtime.notify_all(
                f"{self.name}: Jev unreachable, handing control back to your automations."
            )
        self._notify_entities()

    async def _succeeded(self) -> None:
        self.failures = 0
        if self.degraded:
            _LOGGER.info("Jev reachable again for %s, taking control back", self.name)
            self.degraded = False
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            await self._take_automations()

    # --- takeover ---

    def _when_started(self, job: Any) -> bool:
        """Defer until Home Assistant has started, when automations exist."""
        if self.hass.state is CoreState.running:
            return False

        async def _later(_hass: HomeAssistant) -> None:
            await job()

        self._unsubs.append(async_at_started(self.hass, _later))
        return True

    async def _take_automations(self) -> None:
        if not self.in_control or self._when_started(self._take_automations):
            return
        async with self._automations_lock:
            for automation in self.data.get(CONF_YIELD, []):
                # Control can end while a turn_off is in flight; stop taking then.
                if not self.in_control:
                    return
                state = self.hass.states.get(automation)
                if state is None or state.state != "on":
                    continue
                # Recorded before the call: if the call is cancelled or the room
                # stops mid-flight, release still knows to turn it back on.
                taken = self.runtime.taken.setdefault(self.subentry_id, [])
                if automation not in taken:
                    taken.append(automation)
                self.runtime.async_save()
                if not await self._call_automation("turn_off", automation):
                    taken.remove(automation)
                    self.runtime.async_save()

    async def _release_automations(self) -> None:
        if not self._stopped and self._when_started(self._release_automations):
            return
        async with self._automations_lock:
            # Checked under the lock: a take in flight may still be recording.
            if self.runtime.taken.get(self.subentry_id):
                await self.runtime.async_release(self.subentry_id)

    async def _call_automation(self, service: str, entity_id: str) -> bool:
        try:
            await self.hass.services.async_call(
                "automation", service, {"entity_id": entity_id}, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning("automation.%s %s failed: %s", service, entity_id, err)
            return False
        return True

    @callback
    def _notify_entities(self) -> None:
        async_dispatcher_send(self.hass, self.signal)
