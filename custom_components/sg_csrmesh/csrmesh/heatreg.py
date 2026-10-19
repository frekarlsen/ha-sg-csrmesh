"""HeatReg Smart (SG type ``ThermostatGen3``): status, temperatures and commands.

Verified on 19.10.2026 on three thermostats driven by the SG gateway (see
docs/gen1-protocol.md 6.1), cross-checked against the values the gateway
shows through HomeKit:

    FF04                      -> FF05 <flags> <sp> <sp/10> <fw a> <fw b> ..
    FF3C 00                   -> FF3D <room> <room/10> <floor> <floor/10> <max> <max/10> ..
    FF3C 01                   -> FF5C <calibration, base heat, sensor type ...>
    FF3E <int> <tenths> 01 <tid>  set-point            -> FF08 3E
    FF42 <on> 00 <tid>            on / off             (seen in the next FF05)

Flags: bit 0 = on, bit 5 = heating now (relay closed). The app chooses room or
floor temperature by a sensor mode it keeps itself; it is not in the status.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import OP_STATUS, SG_TAG, Message, _addr

OP_TEMPERATURE_GET = 0xFF3C
OP_TEMPERATURES = 0xFF3D
OP_SETPOINT = 0xFF3E
OP_POWER = 0xFF42
OP_SETTINGS = 0xFF5C

TEMP_MIN = 5.0   # the range the gateway offers through HomeKit
TEMP_MAX = 35.0

FLAG_ON = 0x01
FLAG_HEATING = 0x20


def _ext(mesh_id: int, opcode: int, tag: bytes, data: bytes) -> bytes:
    return _addr(mesh_id) + opcode.to_bytes(2, "big") + tag + data


def setpoint_cmd(mesh_id: int, celsius: float, tid: int, tag: bytes = SG_TAG) -> bytes:
    """FF3E: new set-point, whole degrees and tenths."""
    tenths = round(min(TEMP_MAX, max(TEMP_MIN, celsius)) * 10)
    return _ext(mesh_id, OP_SETPOINT, tag, bytes([tenths // 10, tenths % 10, 0x01, tid & 0xFF]))


def power_cmd(mesh_id: int, on: bool, tid: int, tag: bytes = SG_TAG) -> bytes:
    """FF42: switch the thermostat on or off."""
    return _ext(mesh_id, OP_POWER, tag, bytes([1 if on else 0, 0x00, tid & 0xFF]))


def temperatures_get_cmd(mesh_id: int, tag: bytes = SG_TAG) -> bytes:
    """FF3C 00: ask for room and floor temperature (answer FF3D)."""
    return _ext(mesh_id, OP_TEMPERATURE_GET, tag, b"\x00")


def _temp(whole: int, tenths: int) -> float | None:
    return None if tenths > 9 else round(whole + tenths / 10, 1)


@dataclass(frozen=True, slots=True)
class ThermostatStatus:
    on: bool
    heating: bool
    target: float
    firmware: str


def parse_status(msg: Message) -> ThermostatStatus | None:
    """FF05 from a thermostat (only call this for thermostats)."""
    b = msg.body
    if msg.opcode != OP_STATUS or len(b) < 5:
        return None
    target = _temp(b[1], b[2])
    if target is None:
        return None
    return ThermostatStatus(
        on=bool(b[0] & FLAG_ON),
        heating=bool(b[0] & FLAG_HEATING),
        target=target,
        firmware=f"{b[3]}.{b[4]}",
    )


@dataclass(frozen=True, slots=True)
class Temperatures:
    room: float | None
    floor: float | None
    max_floor: float | None


def parse_temperatures(msg: Message) -> Temperatures | None:
    b = msg.body
    if msg.opcode != OP_TEMPERATURES or len(b) < 6:
        return None
    return Temperatures(_temp(b[0], b[1]), _temp(b[2], b[3]), _temp(b[4], b[5]))
