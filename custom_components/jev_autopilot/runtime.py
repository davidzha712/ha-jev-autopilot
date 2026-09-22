"""Shared state for the service entry: client, budget, confirmations, audit log."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from jevclient import (
    USD_PER_MILLION_INPUT_TOKENS,
    JevClient,
    JevConnectionError,
    JevOverloadedError,
    JevRateLimitError,
    JevResponse,
)

from .const import (
    ACTION_RUN,
    ACTION_SKIP,
    CONF_BUDGET,
    CONF_NOTIFY,
    CONFIRM_TTL_S,
    DEFAULT_BUDGET,
    DOMAIN,
    LOG_SIZE,
    SKIP_SUPPRESS_S,
)
from .engine import Action, Question

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .controller import RoomController

_LOGGER = logging.getLogger(__package__)
SIGNAL_GLOBAL = f"{DOMAIN}_global"
MAX_RETRY_WAIT_S = 30.0


def entry_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.{entry_id}")


async def async_release_stored(hass: HomeAssistant, entry_id: str) -> None:
    """On entry removal: hand back whatever is still held, then delete the file."""
    store = entry_store(hass, entry_id)
    data = await store.async_load() or {}
    left: list[str] = []
    for automations in data.get("taken", {}).values():
        for automation in automations:
            if hass.states.get(automation) is None:
                left.append(automation)
                continue
            try:
                await hass.services.async_call(
                    "automation", "turn_on", {"entity_id": automation}, blocking=True
                )
            except HomeAssistantError as err:
                _LOGGER.warning("automation.turn_on %s failed: %s", automation, err)
                left.append(automation)
    if left:
        # Nothing of this integration is left to retry later, so a person must.
        persistent_notification.async_create(
            hass,
            "Jev Autopilot was removed but could not turn these automations back on. "
            "Turn them on yourself if you still want them:\n"
            + "\n".join(f"- {a}" for a in sorted(set(left))),
            title="Jev Autopilot: automations still off",
            notification_id=f"{DOMAIN}_{entry_id}_left_off",
        )
    await store.async_remove()


@dataclass
class Pending:
    action: Action
    room: RoomController
    expires: float
    name: str


class Runtime:
    """Everything the rooms share."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: JevClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.rooms: dict[str, RoomController] = {}
        self.calls_today = 0
        self.cost_today = 0.0
        self._day = dt_util.now().date()
        self.pending: dict[str, Pending] = {}
        self.suppressed: dict[tuple[str, str], float] = {}
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self.taken: dict[str, list[str]] = {}
        self.presets: dict[str, str] = {}
        # Home Assistant users' names, kept out of prompts like residents' names.
        self.user_names: list[str] = []
        self._closed = False
        self._store = entry_store(hass, entry.entry_id)
        self._sleep: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep

    # --- persistence ---

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.log.extend(data.get("log", []))
        self.taken = {k: list(v) for k, v in data.get("taken", {}).items()}
        self.presets = dict(data.get("presets", {}))
        self.user_names = [
            user.name
            for user in await self.hass.auth.async_get_users()
            if user.name and not user.system_generated
        ]
        counters = data.get("counters", {})
        if counters.get("day") == self._day.isoformat():
            self.calls_today = int(counters.get("calls", 0))
            self.cost_today = float(counters.get("cost", 0.0))

    @callback
    def async_save(self) -> None:
        # After unload a new Runtime owns the same file; a late write would clobber it.
        if not self._closed:
            self._store.async_delay_save(self._data, 10)

    def _data(self) -> dict[str, Any]:
        return {
            "log": list(self.log),
            "taken": self.taken,
            "presets": self.presets,
            "counters": {
                "day": self._day.isoformat(),
                "calls": self.calls_today,
                "cost": self.cost_today,
            },
        }

    async def async_flush(self) -> None:
        """Final write on unload. Nothing is saved by this Runtime afterwards."""
        # Closed before the await, so nothing can schedule a write behind this one.
        self._closed = True
        await self._store.async_save(self._data())

    # --- automations handed back ---

    async def async_release(self, owner: str) -> None:
        """Turn back on what `owner` turned off. Keep what cannot be found yet.

        Each automation leaves the record only once it is back on, so a release
        cancelled halfway still knows what is left.
        """
        held = self.taken.get(owner, [])
        for automation in list(held):
            if self.hass.states.get(automation) is None:
                continue
            try:
                await self.hass.services.async_call(
                    "automation", "turn_on", {"entity_id": automation}, blocking=True
                )
            except HomeAssistantError as err:
                _LOGGER.warning("automation.turn_on %s failed: %s", automation, err)
                continue
            held.remove(automation)
            self.async_save()
        if not held:
            self.taken.pop(owner, None)
        self.async_save()

    async def async_release_orphans(self) -> None:
        """Hand back automations held for rooms that no longer exist."""
        for owner in [o for o in self.taken if o not in self.rooms]:
            await self.async_release(owner)

    # --- budget and calls ---

    @property
    def budget(self) -> int:
        return int(self.entry.options.get(CONF_BUDGET, DEFAULT_BUDGET))

    def _roll_day(self) -> None:
        today: date = dt_util.now().date()
        if today != self._day:
            self._day = today
            self.calls_today = 0
            self.cost_today = 0.0
            ir.async_delete_issue(self.hass, DOMAIN, "budget_reached")

    def budget_left(self) -> bool:
        self._roll_day()
        if self.calls_today < self.budget:
            return True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            "budget_reached",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="budget_reached",
            translation_placeholders={"budget": str(self.budget)},
        )
        return False

    async def ask(self, state: str, questions: Mapping[str, Question]) -> JevResponse:
        """One call, with a single retry for transient failures."""
        for attempt in (1, 2):
            try:
                response = await self.client.ask(state, dict(questions))
            except JevRateLimitError as err:
                if attempt == 2:
                    raise
                await self._sleep(min(err.retry_after or 5.0, MAX_RETRY_WAIT_S))
            except (JevOverloadedError, JevConnectionError):
                if attempt == 2:
                    raise
                await self._sleep(2.0)
            else:
                self._count(response)
                return response
        raise AssertionError("unreachable")

    def _count(self, response: JevResponse) -> None:
        self._roll_day()
        self.calls_today += 1
        self.cost_today += (
            response.usage.input_tokens * USD_PER_MILLION_INPUT_TOKENS / 1e6
        )
        async_dispatcher_send(self.hass, SIGNAL_GLOBAL)
        self.async_save()

    # --- audit log ---

    @callback
    def record(self, entry: dict[str, Any]) -> None:
        self.log.append(entry)
        self.async_save()

    # --- confirmations ---

    @property
    def notify_services(self) -> list[str]:
        return list(self.entry.options.get(CONF_NOTIFY, []))

    def blocked_keys(self, now: float) -> set[tuple[str, str]]:
        for token in [t for t, p in self.pending.items() if p.expires <= now]:
            del self.pending[token]
        for key in [k for k, until in self.suppressed.items() if until <= now]:
            del self.suppressed[key]
        return {p.action.key for p in self.pending.values()} | set(self.suppressed)

    async def propose(self, room: RoomController, action: Action, name: str) -> bool:
        """Ask the residents. Returns False when there is nobody to ask."""
        targets = self.notify_services
        if not targets:
            return False
        token = secrets.token_hex(6)
        self.pending[token] = Pending(action, room, time.time() + CONFIRM_TTL_S, name)
        zh = self.hass.config.language.lower().startswith("zh")
        verb = _verb(action, zh)
        message = (
            f"{room.name}：{verb}「{name}」？（{action.reason}）"
            if zh
            else f"{room.name}: {verb} “{name}”? ({action.reason})"
        )
        payload = {
            "title": "Jev Autopilot",
            "message": message,
            "data": {
                "tag": f"jevap_{token}",
                "actions": [
                    {"action": f"{ACTION_RUN}{token}", "title": "执行" if zh else "Run"},
                    {
                        "action": f"{ACTION_SKIP}{token}",
                        "title": "忽略" if zh else "Skip",
                    },
                ],
            },
        }
        for target in targets:
            await self._notify(target, payload)
        return True

    async def _notify(self, target: str, payload: Mapping[str, Any]) -> None:
        service = target.removeprefix("notify.")
        try:
            await self.hass.services.async_call("notify", service, dict(payload))
        except Exception:
            _LOGGER.warning("Notify service notify.%s failed", service, exc_info=True)

    async def notify_all(self, message: str) -> None:
        for target in self.notify_services:
            await self._notify(target, {"title": "Jev Autopilot", "message": message})

    async def async_handle_action(self, event: Event) -> None:
        action = str(event.data.get("action", ""))
        if action.startswith(ACTION_RUN):
            token, run = action.removeprefix(ACTION_RUN), True
        elif action.startswith(ACTION_SKIP):
            token, run = action.removeprefix(ACTION_SKIP), False
        else:
            return
        pending = self.pending.pop(token, None)
        if pending is None:
            return
        for target in self.notify_services:
            await self._notify(
                target,
                {"message": "clear_notification", "data": {"tag": f"jevap_{token}"}},
            )
        if pending.expires <= time.time() or pending.room.stopped:
            return
        if run:
            await pending.room.async_execute(pending.action, confirmed=True)
        else:
            self.suppressed[pending.action.key] = time.time() + SKIP_SUPPRESS_S
            pending.room.record_veto(pending.action)


def _verb(action: Action, zh: bool) -> str:
    verbs = {
        "lock": ("锁上", "Lock"),
        "turn_on": ("打开", "Turn on"),
        "turn_off": ("关闭", "Turn off"),
    }
    zh_word, en_word = verbs.get(action.service, (action.service, action.service))
    return zh_word if zh else en_word
