"""Config flow: the service entry, its options, reauth/reconfigure and rooms."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from jevclient import MAX_SCORE_LEVELS, JevAuthError, JevClient, JevError, Noul

from .const import (
    CONF_AREA,
    CONF_BASE_URL,
    CONF_BRIGHTNESS_LEVELS,
    CONF_BUDGET,
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
    CONF_MODEL,
    CONF_NOTIFY,
    CONF_PATROL,
    CONF_PRESET,
    CONF_ROOM_NOTES,
    CONF_SWITCHES,
    CONF_YIELD,
    DEFAULT_BASE_URL,
    DEFAULT_BRIGHTNESS_LEVELS,
    DEFAULT_BUDGET,
    DEFAULT_COLOR_TEMP_LEVELS,
    DEFAULT_CONFIRM_THRESHOLD,
    DEFAULT_HEATING_LEVELS,
    DEFAULT_MODEL,
    DEFAULT_PATROL,
    DOMAIN,
    MIN_CONFIRM_THRESHOLD,
    MIN_PATROL,
    OPENROUTER_URL,
    REQUEST_TIMEOUT_S,
    SUBENTRY_ROOM,
)
from .engine import DEFAULT_PRESET, PRESETS, parse_heating_levels, parse_int_levels


async def _validate(hass: Any, data: Mapping[str, Any]) -> str | None:
    """Spend one tiny call to prove the key and endpoint work. Returns an error key."""
    client = JevClient(
        data[CONF_API_KEY],
        session=async_get_clientsession(hass),
        base_url=data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        model=data.get(CONF_MODEL, DEFAULT_MODEL),
        timeout=REQUEST_TIMEOUT_S,
    )
    try:
        await client.ask(
            "Home Assistant is checking that it can reach this service.",
            {"ok": Noul("Is this a connection test?")},
        )
    except JevAuthError:
        return "invalid_auth"
    except JevError:
        return "cannot_connect"
    return None


def _setup_schema(defaults: Mapping[str, Any], *, with_key: bool = True) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_key:
        fields[vol.Required(CONF_API_KEY)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    fields[
        vol.Required(CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL))
    ] = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
    )
    fields[vol.Required(CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL))] = (
        str
    )
    return vol.Schema(fields)


class JevAutopilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the service entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JevAutopilotOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ROOM: RoomSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_BASE_URL] = user_input[CONF_BASE_URL].rstrip("/")
            if (error := await _validate(self.hass, user_input)) is None:
                return self.async_create_entry(title="Jev Autopilot", data=user_input)
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _setup_schema({}), user_input or {}
            ),
            errors=errors,
            description_placeholders={"openrouter_url": OPENROUTER_URL},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            if (error := await _validate(self.hass, data)) is None:
                return self.async_update_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            data[CONF_BASE_URL] = data[CONF_BASE_URL].rstrip("/")
            if not user_input.get(CONF_API_KEY):
                data[CONF_API_KEY] = entry.data[CONF_API_KEY]
            if (error := await _validate(self.hass, data)) is None:
                return self.async_update_and_abort(entry, data=data)
            errors["base"] = error
        schema = _setup_schema(entry.data, with_key=False).extend(
            {
                vol.Optional(CONF_API_KEY): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )


def _notify_options(hass: Any) -> list[str]:
    return sorted(hass.services.async_services_for_domain("notify"))


class JevAutopilotOptionsFlow(OptionsFlow):
    """Global behaviour: preset, confirmations, budget and level templates."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            for key, parser in (
                (CONF_BRIGHTNESS_LEVELS, parse_int_levels),
                (CONF_COLOR_TEMP_LEVELS, parse_int_levels),
                (CONF_HEATING_LEVELS, parse_heating_levels),
            ):
                try:
                    parsed = parser(user_input[key])
                except ValueError:
                    errors[key] = "invalid_levels"
                    continue
                # Brightness is asked as a score, which the API caps at ten levels.
                if key == CONF_BRIGHTNESS_LEVELS and len(parsed) > MAX_SCORE_LEVELS:
                    errors[key] = "too_many_levels"
                elif key == CONF_BRIGHTNESS_LEVELS and not all(
                    1 <= int(value) <= 100 for value in parsed
                ):
                    errors[key] = "brightness_range"
            if not errors:
                return self.async_create_entry(data=user_input)

        current = {
            CONF_PRESET: DEFAULT_PRESET,
            CONF_NOTIFY: [],
            CONF_CONFIRM_THRESHOLD: DEFAULT_CONFIRM_THRESHOLD,
            CONF_BUDGET: DEFAULT_BUDGET,
            CONF_PATROL: DEFAULT_PATROL,
            CONF_BRIGHTNESS_LEVELS: DEFAULT_BRIGHTNESS_LEVELS,
            CONF_COLOR_TEMP_LEVELS: DEFAULT_COLOR_TEMP_LEVELS,
            CONF_HEATING_LEVELS: DEFAULT_HEATING_LEVELS,
            CONF_HOUSE_NOTES: "",
            **self.config_entry.options,
            **(user_input or {}),
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_PRESET): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(PRESETS),
                        translation_key="preset",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(CONF_NOTIFY): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_notify_options(self.hass),
                        multiple=True,
                        custom_value=True,
                    )
                ),
                vol.Required(CONF_CONFIRM_THRESHOLD): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_CONFIRM_THRESHOLD, max=0.99, step=0.01
                    )
                ),
                vol.Required(CONF_BUDGET): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=50, max=100000, step=50, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_PATROL): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_PATROL, max=60, step=1, unit_of_measurement="min"
                    )
                ),
                vol.Required(CONF_BRIGHTNESS_LEVELS): str,
                vol.Required(CONF_COLOR_TEMP_LEVELS): str,
                vol.Required(CONF_HEATING_LEVELS): str,
                vol.Optional(CONF_HOUSE_NOTES): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            errors=errors,
        )


def _entities(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=True)
    )


def _room_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Optional(CONF_AREA): selector.AreaSelector(),
            vol.Optional(CONF_LIGHTS): _entities("light"),
            vol.Optional(CONF_SWITCHES): _entities(["switch", "input_boolean"]),
            vol.Optional(CONF_FANS): _entities("fan"),
            vol.Optional(CONF_CLIMATES): _entities("climate"),
            vol.Optional(CONF_MEDIA): _entities("media_player"),
            vol.Optional(CONF_CONFIRM_ENTITIES): _entities(
                ["lock", "switch", "input_boolean"]
            ),
            vol.Optional(CONF_CONTEXT): _entities(
                ["sensor", "binary_sensor", "input_boolean", "input_select"]
            ),
            vol.Optional(CONF_YIELD): _entities("automation"),
            vol.Optional(CONF_ROOM_NOTES): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        }
    )


_ROOM_LISTS = (CONF_LIGHTS, CONF_SWITCHES, CONF_FANS, CONF_CLIMATES, CONF_MEDIA)


def _room_entities(data: Mapping[str, Any]) -> set[str]:
    return {e for key in (*_ROOM_LISTS, CONF_CONFIRM_ENTITIES) for e in data.get(key, [])}


def _room_error(
    data: Mapping[str, Any], others: Iterable[Mapping[str, Any]] = ()
) -> str | None:
    controlled = [e for key in _ROOM_LISTS for e in data.get(key, [])]
    confirm = set(data.get(CONF_CONFIRM_ENTITIES, []))
    if not controlled and not confirm:
        return "no_entities"
    if len(controlled) != len(set(controlled)) or confirm & set(controlled):
        return "duplicate_entity"
    # Two rooms driving one device would fight over it.
    taken = set().union(*(_room_entities(o) for o in others))
    if _room_entities(data) & taken:
        return "entity_in_other_room"
    # Two rooms yielding one automation: the first to let go would turn it back on
    # while the other still runs the room.
    yielded = set().union(*(set(o.get(CONF_YIELD, [])) for o in others))
    if set(data.get(CONF_YIELD, [])) & yielded:
        return "automation_in_other_room"
    return None


class RoomSubentryFlow(ConfigSubentryFlow):
    """One room: what it controls, what it looks at, which automations it replaces."""

    def _other_rooms(self, skip: str | None = None) -> list[Mapping[str, Any]]:
        return [
            sub.data
            for sub in self._get_entry().subentries.values()
            if sub.subentry_type == SUBENTRY_ROOM and sub.subentry_id != skip
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if (error := _room_error(user_input, self._other_rooms())) is None:
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=user_input
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _room_schema(), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                error := _room_error(user_input, self._other_rooms(subentry.subentry_id))
            ) is None:
                # The entry's update listener reloads it; update_reload_and_abort
                # refuses to run while one is registered.
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=user_input[CONF_NAME],
                    data=user_input,
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _room_schema(), user_input or dict(subentry.data)
            ),
            errors=errors,
        )
