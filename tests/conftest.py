"""Fixtures. Nothing here talks to TypeSafe: the client is replaced everywhere."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_API_KEY, CONF_NAME
from jevclient import JevResponse, NoulAnswer, Usage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jev_autopilot.const import (
    CONF_BASE_URL,
    CONF_CONFIRM_ENTITIES,
    CONF_CONTEXT,
    CONF_LIGHTS,
    CONF_MODEL,
    CONF_NOTIFY,
    CONF_YIELD,
    DOMAIN,
    SUBENTRY_ROOM,
)

API_KEY = "test-key-not-a-real-one"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Without this the custom component is never loaded."""
    return


class FakeJev:
    """Answers every question by its kind prefix (on_0, bri_1, lock_2, ...)."""

    def __init__(self) -> None:
        self.policy: dict[str, Any] = {"on": NoulAnswer(0.95), "lock": NoulAnswer(0.95)}
        self.error: Exception | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ask = AsyncMock(side_effect=self._ask)
        self.async_close = AsyncMock()

    async def _ask(self, state: str, questions: dict[str, Any]) -> JevResponse:
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        answers = {
            key: self.policy[key.split("_")[0]]
            for key in questions
            if key.split("_")[0] in self.policy
        }
        return JevResponse(
            model="jev-1.13.0",
            answers=answers,
            usage=Usage(input_tokens=1000, output_tokens=10),
            latency_ms=120.0,
        )


@pytest.fixture
def jev():
    fake = FakeJev()
    with (
        patch("custom_components.jev_autopilot.JevClient", return_value=fake),
        patch("custom_components.jev_autopilot.config_flow.JevClient", return_value=fake),
    ):
        yield fake


@pytest.fixture(autouse=True)
def no_retry_sleep():
    with patch("custom_components.jev_autopilot.runtime.asyncio.sleep", new=AsyncMock()):
        yield


def room(name: str = "Living room", **data: Any) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={CONF_NAME: name, **data},
        subentry_type=SUBENTRY_ROOM,
        title=name,
        unique_id=None,
    )


def make_entry(*subentries: ConfigSubentryData, **options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Jev Autopilot",
        data={
            CONF_API_KEY: API_KEY,
            CONF_BASE_URL: "https://example.invalid",
            CONF_MODEL: "jev-test",
        },
        options={CONF_NOTIFY: ["notify.phone"], **options},
        subentries_data=list(subentries),
    )


DEFAULT_ROOM = {
    CONF_LIGHTS: ["light.ceiling"],
    CONF_CONFIRM_ENTITIES: ["lock.front"],
    CONF_CONTEXT: ["binary_sensor.motion"],
    CONF_YIELD: ["automation.old_lights"],
}
