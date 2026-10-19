"""0.7.0: Smart Switch panel at setup, pairing flow, «Styrer» / «Styres av»."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from helpers import answering, inject, make_entry, sent_payloads, setup
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

SWITCH = {"name": "Bryter", "kind": "switch", "model": "SmartSwitchV2", "tag": "0000"}
LIGHT = {"name": "Vaskerom", "kind": "dimmer", "model": "DimmerGen4", "tag": "0000"}
GATEWAY_LIGHT = {"name": "Nattbord", "kind": "dimmer", "model": "MicroPillGen4", "tag": "e7a4"}


def _switch_acks(p: bytes) -> list[tuple[int, str]]:
    """What c3e4 answered on 19.10.2026 (docs 6.1)."""
    if p[:2] != bytes.fromhex("e4c3"):
        return []
    op, data = p[2:4], p[6:]
    if op == b"\xff\x12":
        return [(0xC3E4, "0080ff08000012")]
    if op == b"\xff\x17":
        return [(0xC3E4, f"0080ff08000017{data[0]:02x}")]
    if op == b"\xff\x13":
        return [(0xC3E4, f"0080ff08000013{data[0]:02x}{data[1]:02x}")]
    return []


def _to_switch(send) -> list[str]:
    return [p.hex() for p in sent_payloads(send) if p[:2] == bytes.fromhex("e4c3")]


async def _menu(hass, entry, step: str):
    r = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": step})


async def test_add_switch_asks_for_panel(hass: HomeAssistant, send):
    entry = make_entry()
    await setup(hass, entry)
    r = await _menu(hass, entry, "add_by_address")
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"mesh_id": "c3e4", "name": "Bryter", "model": "SmartSwitchV2"}
    )
    assert r["step_id"] == "switch_panel"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"panel": "one_rocker"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["devices"]["c3e4"] == {
        "name": "Bryter", "kind": "switch", "model": "SmartSwitchV2", "panel": "one_rocker"
    }


async def test_pair_and_unpair_left_rocker(hass: HomeAssistant, send):
    entry = make_entry({"c3e4": SWITCH, "9640": LIGHT, "89e0": GATEWAY_LIGHT})
    await setup(hass, entry)
    coord = entry.runtime_data
    send.side_effect = answering(coord, _switch_acks)

    r = await _menu(hass, entry, "pair_switch")
    assert r["step_id"] == "pair_target"  # only one switch: straight to the rocker
    lights = next(f for f in r["data_schema"].schema if f == "target")
    options = r["data_schema"].schema[lights].config["options"]
    assert [o["value"] for o in options] == ["9640"]  # 89e0 has the gateway's tag
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"action": "pair", "rocker": "left", "target": "9640"}
    )
    assert r["step_id"] == "pair_press"
    with patch("custom_components.sg_csrmesh.coordinator.SWITCH_ACK_SETTLE", 0.01):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        assert r["type"] is FlowResultType.SHOW_PROGRESS
        await asyncio.sleep(0)
        inject(coord, 0xC3E4, "0080ff150000000101")  # the user presses
        await hass.async_block_till_done(wait_background_tasks=True)
        r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "paired"
    assert entry.options["devices"]["c3e4"]["pairs"] == {"left": "9640"}
    sent = _to_switch(send)
    assert [s[4:8] + s[12:14] for s in sent] == [
        "ff1201", "ff1700", "ff1300", "ff1200",  # wake, clear, set (button 0), sleep
    ]
    assert sent[2][12:20] == "00009640"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bryter_controls").state == "Vaskerom"
    assert hass.states.get("sensor.vaskerom_controlled_by").state == "Bryter"

    # Remove it again: clear only.
    send.reset_mock()
    r = await _menu(hass, entry, "pair_switch")
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"action": "unpair", "rocker": "left"}
    )
    with patch("custom_components.sg_csrmesh.coordinator.SWITCH_ACK_SETTLE", 0.01):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        await asyncio.sleep(0)
        inject(coord, 0xC3E4, "0080ff150000000101", 2)
        await hass.async_block_till_done(wait_background_tasks=True)
        r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["reason"] == "unpaired"
    assert entry.options["devices"]["c3e4"]["pairs"] == {}
    assert [s[4:8] for s in _to_switch(send)] == ["ff12", "ff17", "ff12"]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.vaskerom_controlled_by").state == "–"


async def test_big_rocker_configures_both_buttons_across_presses(hass: HomeAssistant, send):
    """A unit not acknowledged in one wake window goes out at the next press."""
    entry = make_entry({"c3e4": {**SWITCH, "panel": "one_rocker"}, "9640": LIGHT})
    await setup(hass, entry)
    coord = entry.runtime_data
    dropped = {"first_button_3": True}

    def acks(p):
        if p[2:4] == b"\xff\x13" and p[6] == 3 and dropped["first_button_3"]:
            dropped["first_button_3"] = False  # the switch fell asleep before this
            return []
        return _switch_acks(p)

    send.side_effect = answering(coord, acks)
    r = await _menu(hass, entry, "pair_switch")
    assert r["step_id"] == "pair_target_one"
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"action": "pair", "target": "9640"}
    )
    with patch("custom_components.sg_csrmesh.coordinator.SWITCH_ACK_SETTLE", 0.01):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        await asyncio.sleep(0)
        inject(coord, 0xC3E4, "0080ff150000000101", 1)
        for _ in range(5):
            await asyncio.sleep(0.02)
        assert entry.options["devices"]["c3e4"].get("pairs") is None  # not done yet
        inject(coord, 0xC3E4, "0080ff150000000101", 2)
        await hass.async_block_till_done(wait_background_tasks=True)
        r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["reason"] == "paired"
    assert entry.options["devices"]["c3e4"]["pairs"] == {"all": "9640"}
    ops = [s[4:8] + s[12:14] for s in _to_switch(send)]
    assert ops == [
        "ff1201", "ff1700", "ff1300", "ff1703", "ff1303", "ff1200",  # first press
        "ff1201", "ff1303", "ff1200",  # second: button 3 was cleared already, only the set
    ]


async def test_no_matching_lights(hass: HomeAssistant, send):
    entry = make_entry({"c3e4": SWITCH, "89e0": GATEWAY_LIGHT})
    await setup(hass, entry)
    r = await _menu(hass, entry, "pair_switch")
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "no_matching_lights"


async def test_panel_change_needs_pairings_removed(hass: HomeAssistant, send):
    entry = make_entry({"c3e4": {**SWITCH, "pairs": {"left": "9640"}}, "9640": LIGHT})
    await setup(hass, entry)
    r = await _menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": "c3e4"})
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"model": "SmartSwitchV2", "panel": "one_rocker"}
    )
    assert r["errors"] == {"panel": "panel_has_pairings"}
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"model": "SmartSwitchGen4", "panel": "two_rockers"}
    )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["devices"]["c3e4"]["model"] == "SmartSwitchGen4"
    assert entry.options["devices"]["c3e4"]["pairs"] == {"left": "9640"}


async def test_lost_clear_ack_does_not_hold_up_the_pairing(hass: HomeAssistant, send):
    """19.10.2026: the switch acknowledged FF13 but the FF17 ack never came.

    FF13 is only accepted on a cleared button, so its ack is enough.
    """
    entry = make_entry({"c3e4": SWITCH, "9640": LIGHT})
    await setup(hass, entry)
    coord = entry.runtime_data
    send.side_effect = answering(
        coord, lambda p: [] if p[2:4] == b"\xff\x17" else _switch_acks(p)
    )
    r = await _menu(hass, entry, "pair_switch")
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"action": "pair", "rocker": "right", "target": "9640"}
    )
    with patch("custom_components.sg_csrmesh.coordinator.SWITCH_ACK_SETTLE", 0.01):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {})
        await asyncio.sleep(0)
        inject(coord, 0xC3E4, "0080ff150000030101")
        await hass.async_block_till_done(wait_background_tasks=True)
        r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["reason"] == "paired"
    assert entry.options["devices"]["c3e4"]["pairs"] == {"right": "9640"}
    assert len([s for s in _to_switch(send) if s[4:8] == "ff13"]) == 1  # one press was enough
