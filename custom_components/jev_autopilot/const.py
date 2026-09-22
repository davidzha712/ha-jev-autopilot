"""Constants for Jev Autopilot."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "jev_autopilot"

CONF_BASE_URL: Final = "base_url"
CONF_MODEL: Final = "model"
DEFAULT_BASE_URL: Final = "https://api.typesafe.ai"
OPENROUTER_URL: Final = "https://openrouter.ai/api"
DEFAULT_MODEL: Final = "jev-latest"
REQUEST_TIMEOUT_S: Final = 20.0

# Options
CONF_PRESET: Final = "preset"
CONF_NOTIFY: Final = "notify_services"
CONF_CONFIRM_THRESHOLD: Final = "confirm_threshold"
CONF_BUDGET: Final = "daily_call_budget"
CONF_PATROL: Final = "patrol_minutes"
CONF_BRIGHTNESS_LEVELS: Final = "brightness_levels"
CONF_COLOR_TEMP_LEVELS: Final = "color_temp_levels"
CONF_HEATING_LEVELS: Final = "heating_levels"
CONF_HOUSE_NOTES: Final = "house_notes"

DEFAULT_CONFIRM_THRESHOLD: Final = 0.9
MIN_CONFIRM_THRESHOLD: Final = 0.8
DEFAULT_BUDGET: Final = 3000
DEFAULT_PATROL: Final = 10
MIN_PATROL: Final = 2
DEFAULT_BRIGHTNESS_LEVELS: Final = "10, 30, 50, 75, 100"
DEFAULT_COLOR_TEMP_LEVELS: Final = "2700, 3500, 4000"
DEFAULT_HEATING_LEVELS: Final = "off, 17, 20, 22"

# Room subentry
SUBENTRY_ROOM: Final = "room"
CONF_AREA: Final = "area"
CONF_LIGHTS: Final = "lights"
CONF_SWITCHES: Final = "switches"
CONF_FANS: Final = "fans"
CONF_CLIMATES: Final = "climates"
CONF_MEDIA: Final = "media_players"
CONF_CONFIRM_ENTITIES: Final = "confirm_entities"
CONF_CONTEXT: Final = "context_entities"
CONF_YIELD: Final = "yield_automations"
CONF_ROOM_NOTES: Final = "room_notes"

CONTROLLED_KEYS: Final = (
    CONF_LIGHTS,
    CONF_SWITCHES,
    CONF_FANS,
    CONF_CLIMATES,
    CONF_MEDIA,
    CONF_CONFIRM_ENTITIES,
)

# Runtime
DEBOUNCE_S: Final = 30.0
FAILURES_TO_DEGRADE: Final = 3
CONFIRM_TTL_S: Final = 30 * 60
SKIP_SUPPRESS_S: Final = 2 * 60 * 60
LOG_SIZE: Final = 500
ACTION_RUN: Final = "JEVAP_RUN_"
ACTION_SKIP: Final = "JEVAP_SKIP_"
