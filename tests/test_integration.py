"""Integration tests against a real HA core (pytest-homeassistant-custom-component)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sg_csrmesh.const import DOMAIN
from custom_components.sg_csrmesh.csrmesh import crypto

pytest_plugins = "pytest_homeassistant_custom_component"

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
KEY = crypto.network_key_from_passphrase("1234")


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


@pytest.fixture
def send():
    with patch("custom_components.sg_csrmesh.bridge.MeshLink.send", AsyncMock()) as mock:
        yield mock


def _entry(devices=None, **options):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"passphrase": "1234"},
        options={"devices": devices or {}, "poll_interval": 0, "min_level": 10, **options},
    )


def _sent_payloads(send: AsyncMock) -> list[bytes]:
    out = []
    for call in send.await_args_list:
        frame = crypto.decrypt_packet(KEY, call.args[0])
        assert frame is not None
        out.append(frame.payload)
    return out


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_user_flow(hass: HomeAssistant):
    with patch("custom_components.sg_csrmesh.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert r["type"] is FlowResultType.FORM
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"passphrase": "1234", "network_key": "zz"}
        )
        assert r["errors"] == {"network_key": "invalid_key"}
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"passphrase": "1234", "network_key": ""}
        )
        assert r["type"] is FlowResultType.CREATE_ENTRY
        assert r["options"]["devices"] == {}


async def test_options_add_by_address(hass: HomeAssistant, send):
    entry = _entry()
    await _setup(hass, entry)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.MENU
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"next_step_id": "add_by_address"}
    )
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"mesh_id": "8000", "name": "App", "kind": "dimmer"}
    )
    assert r["errors"] == {"mesh_id": "invalid_mesh_id"}
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"mesh_id": "1A2B", "name": "Stue", "kind": "dimmer"}
    )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["devices"] == {"1a2b": {"name": "Stue", "kind": "dimmer"}}
    assert hass.states.get("light.stue") is not None


async def test_settings_keep_other_options(hass: HomeAssistant, send):
    """Saving settings must not drop options it does not know about."""
    entry = _entry({"1a2b": {"name": "Stue", "kind": "dimmer"}}, future_key={"x": 1})
    await _setup(hass, entry)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "settings"})
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"poll_interval": 60, "min_level": 15}
    )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["poll_interval"] == 60
    assert entry.options["min_level"] == 15
    assert entry.options["future_key"] == {"x": 1}
    assert entry.options["devices"] == {"1a2b": {"name": "Stue", "kind": "dimmer"}}


async def test_light_sends_set_and_reads_status(hass: HomeAssistant, send):
    entry = _entry({"1a2b": {"name": "Stue", "kind": "dimmer", "tag": "e7a4"}})
    await _setup(hass, entry)
    coord = entry.runtime_data

    # The device confirms by poll answer, so the delivery stops after round 1.
    async def answer(packet, **_kw):
        payload = crypto.decrypt_packet(KEY, packet).payload
        if payload[2:4] == b"\xff\x04":
            status = crypto.make_packet(
                KEY, 5, bytes.fromhex("0080ff05e7a40164"), source=0x1A2B
            )
            coord._on_frame(crypto.decrypt_packet(KEY, status))

    send.side_effect = answer
    with patch("custom_components.sg_csrmesh.coordinator.CONFIRM_TIMEOUT", 0.01):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.stue", "brightness": 255}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    sets = [p for p in _sent_payloads(send) if p[2:4] == b"\xff\x03"]
    assert sets, "no FF03 sent"
    assert sets[0][:6] == bytes.fromhex("2b1aff03e7a4")
    assert sets[0][6:8] == b"\x01\x64"  # on, 100 %
    assert coord.stats["confirmed"] == 1

    # A status frame from the dimmer updates the entity (40 % on a 10 % floor).
    status = crypto.make_packet(KEY, 6, bytes.fromhex("0080ff50e7a4012800"), source=0x1A2B)
    coord._on_frame(crypto.decrypt_packet(KEY, status))
    await hass.async_block_till_done(wait_background_tasks=True)
    st = hass.states.get("light.stue")
    assert st.state == "on"
    assert st.attributes["brightness"] == round(1 + (40 - 10) * 254 / 90)

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_switch_press_fires_event_once(hass: HomeAssistant, send):
    entry = _entry({"f866": {"name": "Bryter", "kind": "switch"}})
    await _setup(hass, entry)
    coord = entry.runtime_data
    press = crypto.make_packet(KEY, 7, bytes.fromhex("0080ff15e7a4000101"), source=0xF866)
    for _ in range(3):  # the mesh delivers one press 2-3 times
        coord._on_frame(crypto.decrypt_packet(KEY, press))
    await hass.async_block_till_done(wait_background_tasks=True)
    states = [s for s in hass.states.async_all("event") if s.attributes.get("button") == 0]
    assert len(states) == 1 and states[0].state not in ("unknown", "unavailable")


# --- static checks -------------------------------------------------------------


def _keys(tree, prefix=""):
    out = set()
    for k, v in tree.items():
        path = f"{prefix}.{k}" if prefix else k
        out |= _keys(v, path) if isinstance(v, dict) else {path}
    return out


def test_translations_complete():
    strings = _keys(json.loads((COMPONENT / "strings.json").read_text()))
    for lang in (COMPONENT / "translations").glob("*.json"):
        assert _keys(json.loads(lang.read_text())) == strings, lang.name


def test_version_matches_pyproject():
    import tomllib

    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    pyproject = tomllib.loads((COMPONENT.parents[1] / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"]


async def test_overlapping_captures_keep_logging(hass: HomeAssistant, send):
    """A capture that ends must not switch off one that is still running."""
    import asyncio

    entry = _entry()
    await _setup(hass, entry)
    coord = entry.runtime_data
    release = {1: asyncio.Event(), 2: asyncio.Event()}
    calls = iter((1, 2))

    async def fake_listen(_seconds):
        await release[next(calls)].wait()

    with patch("custom_components.sg_csrmesh.bridge.MeshLink.listen", side_effect=fake_listen):
        first = hass.async_create_task(coord.async_capture(10))
        await asyncio.sleep(0)
        second = hass.async_create_task(coord.async_capture(10))
        await asyncio.sleep(0)
        release[1].set()
        await first
        assert coord._capturing  # the second capture is still running
        release[2].set()
        await second
        assert not coord._capturing
    assert await hass.config_entries.async_unload(entry.entry_id)
