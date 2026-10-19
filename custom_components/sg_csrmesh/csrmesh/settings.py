"""Device settings for SG Gen1 (CSRmesh): trim, fading, push-button input, mode.

Status: mapped from the SG Smart app's own code paths for CSR devices (method
in docs/gen1-protocol.md). Verified on hardware (17.-19.10.2026): reading and
setting trim (FF2A -> FF2B, FF29 -> FF08 29 <kind>, index format, both tags),
fading (FF48 -> FF08 48 <fading>), push-button input (FF47 -> FF08 47 <opt>),
firmware (FF5E -> FF5F) and Smart Switch pairing (FF12/FF17/FF13). Mode (FF39)
is unverified and not used by the integration.

Every SG extension message is ``<dst LE> <opcode BE> <tag> <data...>`` (see
``protocol.py``). The tag is the device's SG "location id" (E7 A4 on devices
set up by the gateway, 00 00 on devices joined by HA); devices ignore a
message with another tag, and answers start with the same tag.

Commands (data after the tag):

    FF29  <kind> <index>        set trim; kind 1 min, 2 start-up, 3 max
    FF2A                        read trim -> FF2B <min> <start> <max> 00 00
    FF39  <mode>                0 dimming, 1 tunable white, 2 on/off,
                                3 countdown, 4 RGB, 5 RGBW, 6 pulse
    FF47  <option>              external push-button input: 0 normal, 1 sensor
    FF48  <fading> <speed>      start-up fading in 1/10 s (app: 0 off, 5, 10,
                                20, 30); dimming speed 0 normal, 1 fast
    FF5E                        read firmware -> FF5F <a b c d e f g>,
                                shown by the app as a.b.c/d.e.f/g

The device acknowledges a setting with FF08 ``<tag> <low byte of the opcode>
...``; for trim the next byte repeats the kind.

Trim values are NOT percent on CSR devices: they are an index into the app's
level tables below (min/start-up: 0..100 -> 0.5..40 %, max: 0..50 -> 40..100 %).
SIG mesh (3.0) devices use half-percent instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .protocol import SG_TAG, Message, _addr

OP_ACK = 0xFF08
OP_TRIM_SET = 0xFF29
OP_TRIM_GET = 0xFF2A
OP_TRIM = 0xFF2B
OP_MODE = 0xFF39
OP_PUSH_SWITCH = 0xFF47
OP_FADING = 0xFF48
OP_FIRMWARE_GET = 0xFF5E
OP_FIRMWARE = 0xFF5F


class TrimKind(IntEnum):
    MIN = 1
    STARTUP = 2
    MAX = 3


class Mode(IntEnum):
    DIMMING = 0
    TUNABLE_WHITE = 1
    ON_OFF = 2
    COUNTDOWN = 3
    RGB = 4
    RGBW = 5
    PULSE = 6


FADING_TENTHS = (0, 5, 10, 20, 30)  # the app's choices (RGBW types: 5 and 30 only)

# The app's level tables for CSR devices (Device.minLevelList / maxLevelList).
# Index on the air -> percent shown in the app.
MIN_LEVELS: tuple[float, ...] = (
    0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
    1.5, 1.5, 1.5, 1.5, 1.5, 2.0, 2.0, 2.0, 2.0, 2.0,
    2.5, 2.5, 2.5, 2.5, 3.0, 3.0, 3.0, 3.0, 3.5, 3.5, 3.5,
    4.0, 4.0, 4.0, 4.5, 5.0, 5.5, 6.5, 7.0, 7.5, 8.0, 9.0, 9.5, 10.0, 10.5,
    11.5, 12.0, 12.5, 13.5, 14.0, 14.5, 15.0, 16.0, 16.5, 17.0, 17.5, 18.5,
    19.0, 19.5, 20.5, 21.0, 21.5, 22.0, 23.0, 23.5, 24.0, 24.5, 25.5, 26.0,
    26.5, 27.5, 28.0, 28.5, 29.0, 30.0, 30.5, 31.0, 31.5, 32.5, 33.0, 33.5,
    34.5, 35.0, 35.5, 36.0, 37.0, 37.5, 38.0, 38.5, 39.5, 40.0,
)
MAX_LEVELS: tuple[float, ...] = (
    40.0, 41.0, 42.0, 44.0, 45.0, 46.0, 47.0, 48.0, 50.0, 51.0, 52.0, 53.0,
    54.0, 56.0, 57.0, 58.0, 59.0, 60.0, 62.0, 63.0, 64.0, 65.0, 66.0, 68.0,
    69.0, 70.0, 71.0, 72.0, 74.0, 75.0, 76.0, 77.0, 78.0, 80.0, 81.0, 82.0,
    83.0, 84.0, 86.0, 87.0, 88.0, 89.0, 90.0, 92.0, 93.0, 94.0, 95.0, 96.0,
    98.0, 99.0, 100.0,
)


def _levels(kind: TrimKind) -> tuple[float, ...]:
    return MAX_LEVELS if kind == TrimKind.MAX else MIN_LEVELS


def trim_index(kind: TrimKind, percent: float) -> int:
    """Percent -> the index the app sends (Device.getSentLevel).

    Walks the table while the next entry is <= percent; an exact hit gives
    the last index with that value, otherwise the first index above it.
    """
    levels = _levels(kind)
    i = 1
    while i < len(levels) and levels[i] <= percent:
        i += 1
    if i == len(levels):
        return i - 1
    return i - 1 if levels[i - 1] == percent else i


def trim_percent(kind: TrimKind, index: int) -> float | None:
    """Index from the device -> percent; None if out of range."""
    levels = _levels(kind)
    return levels[index] if 0 <= index < len(levels) else None


def ext_cmd(mesh_id: int, opcode: int, tag: bytes = SG_TAG, data: bytes = b"") -> bytes:
    """Any SG extension message: <dst LE> <opcode BE> <tag> <data>."""
    return _addr(mesh_id) + opcode.to_bytes(2, "big") + tag + data


def trim_get_cmd(mesh_id: int, tag: bytes = SG_TAG) -> bytes:
    return ext_cmd(mesh_id, OP_TRIM_GET, tag)


def trim_set_cmd(mesh_id: int, kind: TrimKind, index: int, tag: bytes = SG_TAG) -> bytes:
    limit = len(_levels(kind)) - 1
    if not 0 <= index <= limit:
        raise ValueError(f"trim index for {kind.name} must be 0..{limit}")
    return ext_cmd(mesh_id, OP_TRIM_SET, tag, bytes([kind, index]))


def mode_cmd(mesh_id: int, mode: Mode, tag: bytes = SG_TAG) -> bytes:
    return ext_cmd(mesh_id, OP_MODE, tag, bytes([mode]))


def push_switch_cmd(mesh_id: int, sensor: bool, tag: bytes = SG_TAG) -> bytes:
    return ext_cmd(mesh_id, OP_PUSH_SWITCH, tag, bytes([1 if sensor else 0]))


def fading_cmd(mesh_id: int, tenths: int, fast: bool = False, tag: bytes = SG_TAG) -> bytes:
    if not 0 <= tenths <= 0xFF:
        raise ValueError("fading is one byte, in tenths of a second")
    return ext_cmd(mesh_id, OP_FADING, tag, bytes([tenths, 1 if fast else 0]))


def firmware_get_cmd(mesh_id: int, tag: bytes = SG_TAG) -> bytes:
    return ext_cmd(mesh_id, OP_FIRMWARE_GET, tag)


@dataclass(frozen=True, slots=True)
class TrimLevels:
    min_index: int
    startup_index: int
    max_index: int

    @property
    def min_percent(self) -> float | None:
        return trim_percent(TrimKind.MIN, self.min_index)

    @property
    def startup_percent(self) -> float | None:
        return trim_percent(TrimKind.STARTUP, self.startup_index)

    @property
    def max_percent(self) -> float | None:
        return trim_percent(TrimKind.MAX, self.max_index)


def parse_trim(msg: Message) -> TrimLevels | None:
    """FF2B <tag> <min> <start> <max> <?>; the app requires 4 bytes after the tag."""
    if msg.opcode != OP_TRIM or len(msg.body) < 4:
        return None
    return TrimLevels(msg.body[0], msg.body[1], msg.body[2])


@dataclass(frozen=True, slots=True)
class Ack:
    opcode: int   # the command acknowledged, e.g. 0xFF29
    rest: bytes   # what follows (for trim: the kind)


def parse_ack(msg: Message) -> Ack | None:
    """FF08 <tag> <low byte of the acknowledged opcode> ..."""
    if msg.opcode != OP_ACK or not msg.body:
        return None
    return Ack(0xFF00 | msg.body[0], bytes(msg.body[1:]))


def parse_firmware(msg: Message) -> str | None:
    """FF5F <tag> a b c d e f g -> "a.b.c/d.e.f/g" (as the app shows it)."""
    b = msg.body
    if msg.opcode != OP_FIRMWARE or len(b) < 7:
        return None
    return f"{b[0]}.{b[1]}.{b[2]}/{b[3]}.{b[4]}.{b[5]}/{b[6]}"


# --- Smart Switch (battery) ---------------------------------------------------
# Commands to a switch are only heard within about a second of a press.

OP_SWITCH_AWAKE = 0xFF12   # <awake 01|00> <tid> -> FF08 12. Verified 19.10.2026
OP_SWITCH_BUTTON = 0xFF13  # <btn> <type> <target BE> 00 <tid> -> FF08 13 <btn> <type>. Verified
OP_SWITCH_CLEAR = 0xFF17   # <btn> <tid> -> FF08 17 <btn>. Verified 19.10.2026
OP_BATTERY = 0x83          # CSRmesh battery model; 01 = BATTERY_STATE


def switch_awake_cmd(mesh_id: int, awake: bool, tid: int, tag: bytes = SG_TAG) -> bytes:
    """FF12: the app keeps a switch awake (01) while it configures it, then 00."""
    return ext_cmd(mesh_id, OP_SWITCH_AWAKE, tag, bytes([1 if awake else 0, tid & 0xFF]))


def switch_button_cmd(
    mesh_id: int, button: int, target: int, tid: int, kind: int = 0, tag: bytes = SG_TAG
) -> bytes:
    """FF13 as the app builds it.

    One command configures a whole rocker: ``button`` is its top button
    (0 left, 3 right, as in FF15); top turns on and dims up, bottom turns off
    and dims down (kind 00). Kind 07 is single-button dimming (unverified).
    Send it as wake (FF12 01), clear (FF17), FF13, sleep (FF12 00) within
    about 2 s of a press.
    """
    data = bytes([button, kind]) + target.to_bytes(2, "big") + bytes([0, tid & 0xFF])
    return ext_cmd(mesh_id, OP_SWITCH_BUTTON, tag, data)


@dataclass(frozen=True, slots=True)
class Battery:
    level: int   # percent
    state: int
    counter: int


def parse_battery(msg: Message) -> Battery | None:
    """``83 01 <level> <state> <n>``: sent by a switch with every press."""
    b = msg.body
    if msg.opcode != OP_BATTERY or len(b) < 4 or b[0] != 0x01:
        return None
    return Battery(b[1], b[2], b[3])


def switch_clear_cmd(mesh_id: int, button: int, tid: int, tag: bytes = SG_TAG) -> bytes:
    """FF17: clear one button (rocker) before setting it, as the app does."""
    return ext_cmd(mesh_id, OP_SWITCH_CLEAR, tag, bytes([button, tid & 0xFF]))


# --- Devices in the mesh without a location -----------------------------------
# The SG app asks every device with tag 00 00 for its name (FF10 to 0x0000),
# seen 19.10.2026. A device the SG gateway removed keeps the network key and
# only loses its location (tag), so it answers this but not an association
# search: FF11 <tag 00 00> <6 ASCII: type + last 4 of the MAC> <1 byte ?>.

OP_NAME_GET = 0xFF10
OP_NAME = 0xFF11


def name_query_cmd(tag: bytes = b"\x00\x00") -> bytes:
    return ext_cmd(0x0000, OP_NAME_GET, tag)


def parse_name(msg: Message) -> str | None:
    """FF11: the device's advertised name without the @, e.g. "MODBC3"."""
    if msg.opcode != OP_NAME or len(msg.body) < 6:
        return None
    raw = bytes(msg.body[:6])
    return raw.decode("ascii") if all(0x30 <= b < 0x7F for b in raw) else None

