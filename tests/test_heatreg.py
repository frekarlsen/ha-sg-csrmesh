"""HeatReg Smart builders and parsers, against frames captured 19.10.2026 (docs 6.1)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "sg_csrmesh"
sys.path.insert(0, str(ROOT))

from csrmesh import crypto, protocol  # noqa: E402
from csrmesh import heatreg as h  # noqa: E402


def _msg(source: int, payload_hex: str) -> protocol.Message:
    msg = protocol.parse_message(crypto.DecodedFrame(1, source, bytes.fromhex(payload_hex)))
    assert msg is not None
    return msg


def test_status_from_three_thermostats():
    bad = h.parse_status(_msg(0x808C, "0080ff05e7a4911c0034003000"))
    assert bad == h.ThermostatStatus(on=True, heating=False, target=28.0, firmware="52.0")
    kjeller = h.parse_status(_msg(0x8505, "0080ff05e7a4b11b0035023000"))
    assert kjeller.heating and kjeller.target == 27.0 and kjeller.firmware == "53.2"
    gang = h.parse_status(_msg(0xF03F, "0080ff05e7a491180535023000"))
    assert gang.target == 24.5
    off = h.parse_status(_msg(0xF03F, "0080ff05e7a490180035023000"))
    assert not off.on and not off.heating


def test_status_rejects_other_frames():
    assert h.parse_status(_msg(0x808C, "0080ff05e7a49118")) is None  # too short
    assert h.parse_status(_msg(0x808C, "0080ff3de7a415031b081e0000")) is None


def test_temperatures():
    t = h.parse_temperatures(_msg(0x808C, "0080ff3de7a415031b081e0000"))
    assert t == h.Temperatures(room=21.3, floor=27.8, max_floor=30.0)
    t = h.parse_temperatures(_msg(0xF03F, "0080ff3de7a4190118041e0000"))
    assert (t.room, t.floor) == (25.1, 24.4)
    assert h.parse_temperatures(_msg(0x808C, "0080ff3de7a415ff1b081e00")).room is None


def test_commands_as_sent_on_hardware():
    # The gateway (via HomeKit) and HA itself, both acknowledged by f03f.
    assert h.setpoint_cmd(0xF03F, 24.5, 0x00).hex() == "3ff0ff3ee7a418050100"
    assert h.setpoint_cmd(0xF03F, 24.0, 0x31).hex() == "3ff0ff3ee7a418000131"
    assert h.power_cmd(0xF03F, False, 0x32).hex() == "3ff0ff42e7a4000032"
    assert h.power_cmd(0xF03F, True, 0x33).hex() == "3ff0ff42e7a4010033"
    assert h.temperatures_get_cmd(0x808C).hex() == "8c80ff3ce7a400"


def test_setpoint_is_clamped_and_rounded():
    assert h.setpoint_cmd(1, 2.0, 1)[6:8] == bytes([5, 0])
    assert h.setpoint_cmd(1, 50.0, 1)[6:8] == bytes([35, 0])
    assert h.setpoint_cmd(1, 21.26, 1)[6:8] == bytes([21, 3])
    assert h.setpoint_cmd(1, 20.0, 1, tag=protocol.NO_TAG).hex() == "0100ff3e00001400" + "0101"
