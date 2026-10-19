"""0.7.0: models, settings entities, HeatReg, switch panel and battery."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from helpers import answering, inject, make_entry, sent_payloads, setup
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.sg_csrmesh.const import DOMAIN
from custom_components.sg_csrmesh.migrate import MODEL_KIND
from custom_components.sg_csrmesh.models import MODEL_CHOICES, MODELS

DIMMER = {"9640": {"name": "Vaskerom", "kind": "dimmer", "model": "DimmerGen4", "tag": "0000"}}


def _ents(hass: HomeAssistant, entry, domain: str) -> dict[str, er.RegistryEntry]:
    reg = er.async_get(hass)
    return {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(reg, entry.entry_id)
        if e.domain == domain
    }


def test_model_table():
    assert sorted(MODEL_CHOICES) == sorted(MODELS)
    assert set(MODEL_KIND) == set(MODELS)
    assert MODELS["MicroPill"].trim and not MODELS["MicroPill"].fading
    assert not MODELS["Dimmer"].trim
    assert MODELS["DimmerGen4"].fading and MODELS["DimmerGen4"].push_input


async def test_entities_follow_the_model(hass: HomeAssistant, send):
    entry = make_entry({
        **DIMMER,
        "1111": {"name": "Gammel", "kind": "dimmer", "model": "Dimmer"},
        "2222": {"name": "Pille", "kind": "dimmer", "model": "MicroPill"},
    })
    await setup(hass, entry)
    numbers, selects = _ents(hass, entry, "number"), _ents(hass, entry, "select")
    assert {u for u in numbers if "9640" in u} == {
        f"{DOMAIN}_9640_trim_min", f"{DOMAIN}_9640_trim_start_up", f"{DOMAIN}_9640_trim_max"
    }
    assert {u for u in selects if "9640" in u} == {
        f"{DOMAIN}_9640_fading", f"{DOMAIN}_9640_dimming_speed", f"{DOMAIN}_9640_push_switch"
    }
    assert not any("1111" in u for u in [*numbers, *selects])  # older LEDDim: no settings
    assert any("2222" in u for u in numbers) and not any("2222" in u for u in selects)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "9640")})
    assert device.model == "LEDDim Smart (Gen4)" and device.model_id == "DimmerGen4"


async def test_trim_read_and_set(hass: HomeAssistant, send):
    entry = make_entry(DIMMER)
    stored = {"levels": "355432"}

    def reply(p):
        if p[2:4] == b"\xff\x2a":
            return [(0x9640, f"0080ff2b0000{stored['levels']}0000")]
        if p[2:4] == b"\xff\x29":
            kind, idx = p[6], p[7]
            levels = bytearray.fromhex(stored["levels"])
            levels[kind - 1] = idx
            stored["levels"] = levels.hex()
            return [(0x9640, f"0080ff080000 29{kind:02x}".replace(" ", ""))]
        return []

    entry.add_to_hass(hass)
    send.side_effect = answering_for(hass, entry, reply)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coord = entry.runtime_data
    coord.available = True
    coord._notify(coord.devices[0x9640])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("number.vaskerom_maximum_level").state == "100.0"
    assert hass.states.get("number.vaskerom_dimmer_minimum_level").state == "10.0"
    reads = [p for p in sent_payloads(send) if p[2:4] == b"\xff\x2a"]
    assert len(reads) == 1  # three entities, one read

    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.vaskerom_maximum_level", "value": 50},
        blocking=True,
    )
    assert bytes.fromhex("4096ff2900000308") in sent_payloads(send)
    assert hass.states.get("number.vaskerom_maximum_level").state == "50.0"


def answering_for(hass, entry, reply):
    """``answering`` once the coordinator exists (it is made during setup)."""

    async def side_effect(packet, **kw):
        coord = getattr(entry, "runtime_data", None)
        if coord is not None:
            await answering(coord, reply)(packet, **kw)

    return side_effect


async def test_fading_select_stores_what_was_acknowledged(hass: HomeAssistant, send):
    entry = make_entry(DIMMER)
    await setup(hass, entry)
    coord = entry.runtime_data
    coord.available = True
    send.side_effect = answering(
        coord, lambda p: [(0x9640, f"0080ff080000 48{p[6]:02x}".replace(" ", ""))]
        if p[2:4] == b"\xff\x48" else []
    )
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.vaskerom_fading", "option": "fade_30"},
        blocking=True,
    )
    assert bytes.fromhex("4096ff4800001e00") in sent_payloads(send)
    assert entry.options["devices"]["9640"]["fading"] == 30
    assert hass.states.get("select.vaskerom_fading").state == "fade_30"
    # Dimming speed travels with the stored fading.
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.vaskerom_dimming_speed", "option": "fast"}, blocking=True,
    )
    assert sent_payloads(send)[-1] == bytes.fromhex("4096ff4800001e01")


async def test_unacknowledged_setting_raises(hass: HomeAssistant, send):
    entry = make_entry(DIMMER)
    await setup(hass, entry)
    entry.runtime_data.available = True
    with patch("custom_components.sg_csrmesh.coordinator.MeshCoordinator.async_request",
               return_value=None):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "select", "select_option",
                {"entity_id": "select.vaskerom_push_button_input", "option": "sensor"},
                blocking=True,
            )
    assert "push_input" not in entry.options["devices"]["9640"]


async def test_heatreg_climate(hass: HomeAssistant, send):
    entry = make_entry({"f03f": {"name": "Gang", "kind": "thermostat",
                                  "model": "ThermostatGen3", "tag": "e7a4"}})
    await setup(hass, entry)
    coord = entry.runtime_data
    inject(coord, 0xF03F, "0080ff05e7a491180035023000")
    inject(coord, 0xF03F, "0080ff3de7a4190118041e0000")
    await hass.async_block_till_done()
    st = hass.states.get("climate.gang")
    assert st.state == "heat"
    assert st.attributes["temperature"] == 24.0
    assert st.attributes["current_temperature"] == 24.4  # floor, as the gateway shows
    assert st.attributes["hvac_action"] == "idle"
    assert hass.states.get("sensor.gang_room_temperature").state == "25.1"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "f03f")})
    assert device.sw_version == "53.2"

    state = {"flags": 0x91, "sp": "1800"}

    def reply(p):
        if p[2:4] == b"\xff\x3e":
            state["sp"] = p[6:8].hex()
            return [(0xF03F, "0080ff08e7a43e")]
        if p[2:4] == b"\xff\x42":
            state["flags"] = 0x91 if p[6] else 0x90
            return []
        if p[2:4] == b"\xff\x04":
            return [(0xF03F, f"0080ff05e7a4{state['flags']:02x}{state['sp']}35023000")]
        return []

    send.side_effect = answering(coord, reply)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": "climate.gang", "temperature": 22.5},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert any(p[2:4] == b"\xff\x3e" and p[6:8] == bytes([22, 5]) for p in sent_payloads(send))
    assert hass.states.get("climate.gang").attributes["temperature"] == 22.5

    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.gang", "hvac_mode": "off"},
        blocking=True,
    )
    assert hass.states.get("climate.gang").state == "off"


async def test_thermostat_poll_asks_for_temperatures(hass: HomeAssistant, send):
    entry = make_entry({"f03f": {"name": "Gang", "kind": "thermostat",
                                  "model": "ThermostatGen3", "tag": "e7a4"},
                        "c3e4": {"name": "Bryter", "kind": "switch", "tag": "0000"}})
    await setup(hass, entry)
    send.reset_mock()
    await entry.runtime_data._poll()
    payloads = [p.hex() for p in sent_payloads(send)]
    assert payloads == ["3ff0ff04e7a4", "3ff0ff3ce7a400"]  # the sleeping switch is not polled


async def test_one_rocker_panel_events(hass: HomeAssistant, send):
    entry = make_entry({"c3e4": {"name": "Bryter", "kind": "switch", "model": "SmartSwitchV2",
                                  "tag": "0000", "panel": "two_rockers"}})
    await setup(hass, entry)
    assert len(_ents(hass, entry, "event")) == 4

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": {"c3e4": {
            **entry.options["devices"]["c3e4"], "panel": "one_rocker"}}},
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    events = _ents(hass, entry, "event")
    assert set(events) == {f"{DOMAIN}_c3e4_button_top", f"{DOMAIN}_c3e4_button_bottom"}

    coord = entry.runtime_data
    # The big rocker presses buttons 0 and 3 together: one "top" press.
    inject(coord, 0xC3E4, "0080ff150000030101", 1)
    inject(coord, 0xC3E4, "0080ff150000000101", 2)
    await hass.async_block_till_done()
    top = hass.states.get(events[f"{DOMAIN}_c3e4_button_top"].entity_id)
    assert top.attributes["button"] == "top"
    assert top.state not in ("unknown", "unavailable")
    bottom = hass.states.get(events[f"{DOMAIN}_c3e4_button_bottom"].entity_id)
    assert bottom.state == "unknown"


async def test_switch_battery(hass: HomeAssistant, send):
    entry = make_entry({"c3e4": {"name": "Bryter", "kind": "switch", "model": "SmartSwitchV2",
                                  "tag": "0000"}})
    await setup(hass, entry)
    inject(entry.runtime_data, 0xC3E4, "00008301641003")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bryter_battery").state == "100"


async def test_missing_model_repair(hass: HomeAssistant, send):
    entry = make_entry({"ca67": {"name": "Taklys", "kind": "dimmer", "tag": "e7a4"}})
    await setup(hass, entry)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "missing_model_ca67") is not None

    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "edit_device"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": "ca67"})
    assert r["step_id"] == "edit_device_options"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"model": "DimmerGen4"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["devices"]["ca67"]["model"] == "DimmerGen4"
    assert issues.async_get_issue(DOMAIN, "missing_model_ca67") is None
    assert hass.states.get("select.taklys_fading") is not None


async def test_catch_up_asks_again_for_unknown_devices(hass: HomeAssistant, send):
    """Answers right after a connect can be lost: unknown devices are asked again."""
    entry = make_entry({**DIMMER, "c3e4": {"name": "Bryter", "kind": "switch", "tag": "0000"}})
    await setup(hass, entry)
    coord = entry.runtime_data
    send.reset_mock()
    coord._start_catch_up()
    await hass.async_block_till_done(wait_background_tasks=True)
    sent = [p.hex() for p in sent_payloads(send)]
    assert "4096ff040000" in sent and "4096ff2a0000" in sent
    assert not any(p.startswith("e4c3") for p in sent)  # not the sleeping switch

    inject(coord, 0x9640, "0080ff050000005b0001021a00")
    inject(coord, 0x9640, "0080ff2b00003554320000", 2)
    await hass.async_block_till_done(wait_background_tasks=True)
    send.reset_mock()
    coord._start_catch_up()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent_payloads(send) == []  # everything known now


async def test_add_new_nothing_found_shows_menu(hass: HomeAssistant, send):
    """0.6-0.7rc2 crashed here: the menu's step had no handler (UnknownStep)."""
    from homeassistant.data_entry_flow import FlowResultType

    entry = make_entry()
    await setup(hass, entry)
    with patch("custom_components.sg_csrmesh.provision.discover", return_value=[]):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "add_new"}
        )
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        await hass.async_block_till_done(wait_background_tasks=True)
        r = await hass.config_entries.options.async_configure(r["flow_id"])
        assert r["type"] is FlowResultType.MENU and r["step_id"] == "add_new_none"
        # The frontend fetches the current step again: must not raise.
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "add_new"}
        )
        assert r["step_id"] == "add_new"


async def test_add_new_directly(hass: HomeAssistant, send):
    """A device the mesh search cannot reach: connect to it, search and associate there."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.sg_csrmesh.provision import NewDevice

    entry = make_entry()
    await setup(hass, entry)
    link = entry.runtime_data.link
    node = SimpleNamespace(name="@MODBC3", address="00:01:B3:13:DB:C3", rssi=-93)
    pill = NewDevice(bytes(10) + bytes.fromhex("c3db13b30100"), 0x1234ABCD)
    assert pill.mac == "00:01:B3:13:DB:C3"
    pin = AsyncMock()
    with (
        patch.object(type(link), "visible_nodes", lambda self, **kw: [node]),
        patch.object(type(link), "pin", pin),
        patch.object(type(link), "pinned", property(lambda self: "00:01:B3:13:DB:C3")),
        patch("custom_components.sg_csrmesh.provision.discover", AsyncMock(side_effect=[[], [pill]])),
        patch("custom_components.sg_csrmesh.provision.associate", AsyncMock(return_value=0x8765)),
    ):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "add_new"}
        )
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        await hass.async_block_till_done(wait_background_tasks=True)
        r = await hass.config_entries.options.async_configure(r["flow_id"])
        assert r["step_id"] == "add_new_none"
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "add_new_direct"}
        )
        assert r["step_id"] == "add_new_direct"
        async def settle(r):
            # A finished task makes HA go straight on to the next step.
            while r["type"] is FlowResultType.SHOW_PROGRESS:
                await hass.async_block_till_done(wait_background_tasks=True)
                r = await hass.config_entries.options.async_configure(r["flow_id"])
            return r

        r = await settle(await hass.config_entries.options.async_configure(
            r["flow_id"], {"node": "00:01:B3:13:DB:C3"}
        ))
        assert r["step_id"] == "add_new_pick"
        r = await settle(await hass.config_entries.options.async_configure(
            r["flow_id"], {"device": "1234abcd", "name": "Pille", "model": "MicroPill"}
        ))
        assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["devices"]["8765"]["model"] == "MicroPill"
    assert entry.options["devices"]["8765"]["tag"] == "0000"
    calls = [c.args[-1] for c in pin.await_args_list]
    assert "00:01:B3:13:DB:C3" in calls
    assert calls[-1] is None  # released after the key exchange


async def test_pinned_link_uses_only_that_node(hass: HomeAssistant, send):
    from custom_components.sg_csrmesh.bridge import NoMeshNodeError

    entry = make_entry()
    await setup(hass, entry)
    link = entry.runtime_data.link
    link._pinned = "00:01:B3:13:DB:C3"
    target = "custom_components.sg_csrmesh.bridge.bluetooth.async_ble_device_from_address"
    with patch(target, return_value=None), pytest.raises(NoMeshNodeError):
        link._candidates()
    device = object()
    with patch(target, return_value=device):
        assert link._candidates() == [device]


async def test_add_device_removed_from_gateway(hass: HomeAssistant, send):
    """19.10.2026: the gateway's "remove" kept MODBC3 in the mesh as DF73 with tag 00 00.

    It answers the SG app's name query (FF10 -> FF11), not an association search.
    """
    from unittest.mock import AsyncMock

    from homeassistant.data_entry_flow import FlowResultType

    entry = make_entry(DIMMER)
    await setup(hass, entry)
    coord = entry.runtime_data
    send.side_effect = answering(
        coord,
        lambda p: [(0xDF73, "0080ff1100004d4f44424333d8"), (0x9640, "0080ff110000474438333532c3")]
        if p[:4] == bytes.fromhex("0000ff10") else [],
    )
    with patch("custom_components.sg_csrmesh.provision.discover", AsyncMock(return_value=[])):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "add_new"}
        )
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        await hass.async_block_till_done(wait_background_tasks=True)
        if r["type"] is FlowResultType.SHOW_PROGRESS:
            r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["step_id"] == "add_new_pick"
    field = next(f for f in r["data_schema"].schema if f == "device")
    options = r["data_schema"].schema[field].config["options"]
    assert options == [{"value": "mesh:df73", "label": "MODBC3 · DF73"}]  # 9640 is in HA already
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"device": "mesh:df73", "name": "", "model": "MicroPill"}
    )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["devices"]["df73"] == {
        "name": "MODBC3", "kind": "dimmer", "model": "MicroPill", "area_id": None, "tag": "0000"
    }
