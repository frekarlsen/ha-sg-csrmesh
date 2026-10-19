"""Shared fixtures: no radio, every send recorded (see helpers.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _no_bt():
    """No radio: the link never connects and every send is recorded instead."""
    with (
        patch("custom_components.sg_csrmesh.bridge.MeshLink.start"),
        patch("custom_components.sg_csrmesh.bridge.MeshLink.stop", AsyncMock()),
        patch("homeassistant.components.bluetooth.async_setup", return_value=True),
        patch("homeassistant.components.bluetooth.async_setup_entry", return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def _short_waits():
    """Unanswered requests give up quickly: no radio answers in tests."""
    with (
        patch("custom_components.sg_csrmesh.coordinator.REQUEST_WAIT", 0.05),
        patch("custom_components.sg_csrmesh.coordinator.NAME_QUERY_TIME", 0.05),
    ):
        yield


@pytest.fixture
def send():
    with patch("custom_components.sg_csrmesh.bridge.MeshLink.send", AsyncMock()) as mock:
        yield mock
