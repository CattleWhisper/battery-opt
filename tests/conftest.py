"""Shared fixtures for the HA-facing tests (Task 8 onward)."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,  # noqa: ARG001 - fixture dependency
) -> None:
    """Let pytest-homeassistant-custom-component find battery_opt."""
    return
