"""SG Smart first-generation message layer on top of CSRmesh.

Derived from traffic on a live SG Gen1 installation (the owner's own network,
decrypted with its own passphrase) where the SG gateway drives every device:

    gateway -> device   <dst> FF 03 E7 A4 <pwr> <level> <tid> 00 00   set
    gateway -> device   <dst> FF 04 E7 A4                             poll
    device  -> gateway  00 80 FF 05 E7 A4 <body...>                   answer
    device  -> gateway  00 80 FF 50 E7 A4 <pwr> <level> <cnt> ...     change
    dimmer  -> group    FF 7F FF 88 <tag> <pwr> <level> ...           local op

``<dst>`` is the device's 16-bit mesh address, little-endian; a device answers
from that same address. For lights ``<pwr>`` is 00/01 and ``<level>`` is the
dim level in percent (kept while off, so it is the level "on" returns to).
FF03 with pwr 01 and level 00 means "on, at your previous level".

A dimmer operated by hand streams FF88 to group 0x7FFF while it dims and
when it is switched off (seen on a dimmer joined by HA: the off only came
as FF88, never as FF50). It is the device's own state, whatever the
destination. A device joined by HA does not report changes made by a mesh
command at all; only an FF04 poll tells whether a command took effect.

The fiskentnt/com.sgsmart.csrmesh notes describe a different command set
(A0 with F2 E7) for other installations; it is kept below as ``legacy_a0_cmd``
for reference but not used.

Smart Switch presses are below; settings, switch configuration and HeatReg
are in ``settings.py`` and ``heatreg.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .crypto import DecodedFrame, decrypt_packet, make_packet, random_seq

# --- Addresses (little-endian on the air) --------------------------------
ADDR_BROADCAST = 0x0000
ADDR_APP = 0x8000          # gateway/app; devices answer to this address
ADDR_LOCAL_GROUP = 0x7FFF  # a dimmer streams its own manual changes here

# --- GATT ---------------------------------------------------------------
SERVICE_UUID = "0000fef1-0000-1000-8000-00805f9b34fb"
CHAR_8003 = "c4edc000-9daf-11e3-8003-00025b000b00"
CHAR_8004 = "c4edc000-9daf-11e3-8004-00025b000b00"

# Nodes advertise as "@" + two-letter type + four hex digits (@ND, @MD, @MO,
# @ST, @GO, @GD, ...). The hex is the last two MAC bytes, NOT the mesh address.
NODE_NAME = re.compile(r"^@([A-Z]{2})([0-9A-F]{4})$", re.IGNORECASE)

# --- SG extension messages ------------------------------------------------
# Two bytes after every SG extension opcode. Devices set up by the SG
# gateway use E7 A4; a device joined by HA (no gateway involved) uses 00 00.
# It is learned per device from its own messages and echoed in commands.
SG_TAG = b"\xe7\xa4"
NO_TAG = b"\x00\x00"
OP_SET = 0xFF03
OP_POLL = 0xFF04
OP_STATUS = 0xFF05
OP_REPORT = 0xFF50       # sent by a device on its own when its state changes
OP_LOCAL = 0xFF88        # a dimmer operated by hand, to ADDR_LOCAL_GROUP
STATUS_OPS = (OP_STATUS, OP_REPORT)
UNSOLICITED_OPS = (OP_REPORT, OP_LOCAL)
# Destinations whose SG messages carry the sender's own tag.
OWN_TAG_DESTS = (ADDR_APP, ADDR_LOCAL_GROUP)

LEVEL_MIN = 1
LEVEL_MAX = 100


class DeviceKind(StrEnum):
    """What the user says a mesh address is. Drives which entity gets created."""

    DIMMER = "dimmer"            # LEDDim Smart, Smart Pill Micro
    ONOFF = "onoff"              # Smart Pill On/Off
    SWITCH = "switch"            # Smart Switch (buttons -> events)
    THERMOSTAT = "thermostat"    # HeatReg Smart


def is_node_name(local_name: str | None) -> bool:
    return bool(local_name and NODE_NAME.match(local_name.strip()))


def mesh_id_from_name(local_name: str | None) -> int | None:
    """``@MDB8E7`` -> 0xB8E7. Only a label; see NODE_NAME."""
    m = NODE_NAME.match((local_name or "").strip())
    return int(m.group(2), 16) if m else None


def _addr(addr: int) -> bytes:
    return (addr & 0xFFFF).to_bytes(2, "little")


# --- Outbound --------------------------------------------------------------

def set_cmd(mesh_id: int, on: bool, level: int, tid: int, tag: bytes = SG_TAG) -> bytes:
    """FF03: on/off and level, straight to the device's mesh address.

    on=True, level=0 -> "on at previous level" (what the gateway sends).
    """
    level = 0 if not on else max(0, min(LEVEL_MAX, int(level)))
    return (
        _addr(mesh_id)
        + OP_SET.to_bytes(2, "big")
        + tag
        + bytes([1 if on else 0, level, tid & 0xFF, 0x00, 0x00])
    )


def poll_cmd(mesh_id: int, tag: bytes = SG_TAG) -> bytes:
    """FF04: ask one device (or 0x0000 = everyone) for an FF05 answer."""
    return _addr(mesh_id) + OP_POLL.to_bytes(2, "big") + tag


def seal(key: bytes, message: bytes) -> bytes:
    """Wrap a message in an authenticated bearer frame with a fresh seq."""
    return make_packet(key, random_seq(), message)


def legacy_a0_cmd(mesh_id: int, level: int, tid: int) -> bytes:
    """A0 data-model level command from the Homey notes. Not used here."""
    level = max(0, min(LEVEL_MAX, int(level)))
    return (
        b"\x01\x80\x73\xa0"
        + bytes([tid & 0xFF])
        + b"\xf2\xe7"
        + mesh_id.to_bytes(2, "big")
        + bytes([0x00, 0x00, 0x01, level])
    )


# --- Inbound ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Message:
    source: int
    dest: int
    opcode: int
    body: bytes  # after the tag
    tag: bytes | None = None


def parse_message(frame: DecodedFrame) -> Message | None:
    """Split an MCP payload into destination, opcode and body.

    SG extension messages (``FF xx E7 A4``) get a 16-bit opcode 0xFFxx and
    the body after the tag; anything else a one-byte opcode.
    """
    p = frame.payload
    if len(p) < 3:
        return None
    dest = int.from_bytes(p[0:2], "little")
    if len(p) >= 6 and p[2] == 0xFF and p[3] != 0xFF:
        return Message(frame.source, dest, int.from_bytes(p[2:4], "big"), p[6:], bytes(p[4:6]))
    return Message(frame.source, dest, p[2], p[3:])


@dataclass(frozen=True, slots=True)
class LightStatus:
    mesh_id: int
    on: bool
    level: int        # percent; kept while off
    unsolicited: bool  # FF50/FF88: the device changed by itself (by hand)


def parse_light_status(msg: Message) -> LightStatus | None:
    """FF05/FF50 to 0x8000, or FF88 to anyone, starting <pwr 00|01> <level>.

    Thermostat answers start with other values (e.g. 0x91) and are skipped.
    """
    if len(msg.body) < 2:
        return None
    if msg.opcode in STATUS_OPS:
        if msg.dest != ADDR_APP:
            return None
    elif msg.opcode != OP_LOCAL:
        return None
    pwr, level = msg.body[0], msg.body[1]
    if pwr not in (0, 1) or level > LEVEL_MAX:
        return None
    return LightStatus(msg.source, pwr == 1, level, msg.opcode in UNSOLICITED_OPS)


OP_BUTTON = 0xFF15
BUTTONS = 4  # SG Smart Switch: 0 top left, 1 bottom left, 2 bottom right, 3 top right


def parse_switch_event(msg: Message) -> int | None:
    """FF15 <button> 01 01 from a Smart Switch to 0x8000: a button press."""
    if msg.opcode != OP_BUTTON or msg.dest != ADDR_APP or not msg.body:
        return None
    button = msg.body[0]
    return button if button < BUTTONS else None


# Smart Switch V2 (body ``BTN 00 08``/``BTN 00 09``) follows every short press
# with ``02 00 08`` frames for ~7 s, whichever button was pressed (docs 6.6).
V2_TAIL_BUTTON = 2


def is_v2_switch_frame(msg: Message) -> bool:
    """V2 switches send ``BTN 00 xx``; Gen4 (c3e4) sends ``BTN 01 01``."""
    return len(msg.body) >= 2 and msg.body[1] == 0x00


class FrameAssembler:
    """Rebuild frames from 8003/8004 notifications.

    Observed: a frame longer than 20 bytes arrives as a 20-byte 8003 chunk
    followed by the rest on 8004; a frame of up to 20 bytes arrives on 8004
    alone. Chunks of different frames interleave, so every candidate is
    accepted only if its MAC verifies against our key.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._head: bytes | None = None  # pending 8003 chunk

    def reset(self) -> None:
        self._head = None

    def _try(self, data: bytes) -> DecodedFrame | None:
        return decrypt_packet(self._key, data) if len(data) >= 14 else None

    def feed(self, char: str, chunk: bytes) -> list[DecodedFrame]:
        out: list[DecodedFrame] = []
        if char.endswith("8003") or char == CHAR_8003:
            # A new head; the previous one never got its tail. Try it alone.
            if self._head is not None and (f := self._try(self._head)):
                out.append(f)
            self._head = chunk
            return out
        if self._head is not None and (f := self._try(self._head + chunk)):
            self._head = None
            out.append(f)
            return out
        if f := self._try(chunk):
            out.append(f)
        return out
