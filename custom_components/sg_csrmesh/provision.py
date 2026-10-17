"""Add a new (factory reset) SG Gen1 device to the mesh, and remove one.

Uses recsrmesh (LGPL-3.0, github.com/oyvindkinsey/recsrmesh) for the CSRmesh
association protocol (MASP): ECDH key exchange, device-id and network-key
distribution. All MASP traffic goes through our existing GATT link: the node
we are connected to relays it to the new device, which is how the SG app does
it too ("bridge mode"). The new device gets the installation's own network key
(from the passphrase), so it joins the same mesh as everything else.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass

from collections import deque

from recsrmesh.ble import AssociationStateMachine, _process_discovery_packet
from recsrmesh.crypto import (
    DEFAULT_SEQ_ID,
    add_packet_mac,
    derive_key,
    verify_packet_mac,
    xor_masp_packet,
)
from recsrmesh.masp import MaspOpcode
from recsrmesh.protocol import ProtocolState

from .csrmesh.protocol import ADDR_APP, ADDR_BROADCAST, CHAR_8003

_LOGGER = logging.getLogger(__name__)

MASP_KEY = derive_key("", "\x00MASP")
MASP_TTL = 55
DISASSOCIATE_OPCODE = 0x04


class ProvisionError(Exception):
    pass


@dataclass(slots=True)
class NewDevice:
    uuid: bytes
    uuid_hash: int

    @property
    def mac(self) -> str:
        """The UUID ends with the device's BLE MAC, byte-reversed."""
        return ":".join(f"{b:02X}" for b in reversed(self.uuid[-6:]))

    @property
    def label(self) -> str:
        return f"{self.mac} (hash {self.uuid_hash:08X})"


class _LinkClient:
    """The subset of a BleakClient that recsrmesh uses, on top of MeshLink."""

    def __init__(self, link) -> None:  # noqa: D107
        self._link = link
        self._head: bytes | None = None

    async def write_gatt_char(self, char, data, response: bool = False) -> None:
        # recsrmesh writes a long packet as 8003 then 8004; send both chunks
        # back to back under one lock so no mesh frame lands in between.
        if str(char).lower() == CHAR_8003:
            self._head = bytes(data)
            return
        head, self._head = self._head, None
        await self._link.write_packet_raw((head or b"") + bytes(data))

    async def start_notify(self, char, callback) -> None:  # link already listens
        return None

    async def stop_notify(self, char) -> None:
        return None


async def _write_masp(link, message_xored: bytes) -> None:
    packet = add_packet_mac(message_xored, MASP_KEY, ttl=MASP_TTL)
    await link.write_packet_raw(packet)


# Device -> app MASP answers as the bridge delivers them without a MAC
# (observed in captured handshakes): opcode -> length in bytes.
ANSWER_LEN = {0x03: 7, 0x05: 15, 0x07: 14, 0x09: 14, 0x0B: 6, 0x0D: 7}


def _device_answer(raw: bytes) -> bytes | None:
    """The MASP message (still XORed) if ``raw`` is an answer from a device.

    During association the link also carries every ordinary mesh frame and
    the bridge's echo of our own requests. Two shapes are real answers:

    * MAC-protected with the MASP key (the SG bridge does this), and
    * bare, MAC stripped by the bridge, with exactly the known length.

    Requests (even opcodes) and UUID announcements are dropped.
    """
    payload, ok = verify_packet_mac(raw, MASP_KEY)
    if not ok:
        payload = raw
    if not payload:
        return None
    opcode = xor_masp_packet(payload[:1])[0]
    if opcode not in ANSWER_LEN:
        return None
    if not ok and len(payload) != ANSWER_LEN[opcode]:
        return None
    return payload


class _QueuedAssociation(AssociationStateMachine):
    """recsrmesh's state machine, fed from a queue of filtered answers.

    Upstream keeps only the newest packet, so a burst of mesh traffic can
    overwrite the device's answer before it is read.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._answers: deque[bytes] = deque()
        self.seen: list[str] = []

    def feed_raw(self, raw: bytes) -> None:
        if (payload := _device_answer(raw)) is not None:
            msg = xor_masp_packet(payload)
            expected = {
                ProtocolState.WAIT_ASSOC_RESPONSE: 0x03,
                ProtocolState.PUBKEY_EXCHANGE: 0x05,
                ProtocolState.WAIT_CONFIRM_RESPONSE: 0x07,
                ProtocolState.WAIT_RANDOM_RESPONSE: 0x09,
                ProtocolState.WAIT_DEVICE_ID_ACK: 0x0B,
                ProtocolState.NETKEY_EXCHANGE: 0x0D,
            }.get(self.protocol.state)
            if msg[0] != expected:
                return  # an answer, but not the one this step waits for
            self.seen.append(f"{msg[0]:02x}")
            _LOGGER.debug("device answered opcode %02x (%dB)", msg[0], len(msg))
            self._answers.append(msg)

    async def _wait_for_response(self, timeout: float) -> bytes | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._answers:
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.02)
        return self._answers.popleft()


async def discover(link, seconds: float = 6.0) -> list[NewDevice]:
    """Ask every unassociated CSRmesh device in reach to announce itself."""
    results: list[dict] = []
    link.set_raw_sink(lambda raw: _process_discovery_packet(raw, MASP_KEY, results))
    try:
        announce = bytearray(18)
        announce[0] = MaspOpcode.DEVICE_ID_ANNOUNCE
        announce[6:14] = DEFAULT_SEQ_ID
        for _ in range(2):
            await _write_masp(link, xor_masp_packet(bytes(announce)))
            await asyncio.sleep(seconds / 2)
    finally:
        link.set_raw_sink(None)
    return [NewDevice(bytes(r["uuid"]), r["uuid_hash"]) for r in results]


def pick_address(used: set[int]) -> int:
    """A free 16-bit mesh address in the range SG itself uses (high bit set)."""
    reserved = used | {ADDR_APP, 0x8001, 0xFFFF, ADDR_BROADCAST}
    for _ in range(1000):
        addr = 0x8100 + secrets.randbelow(0xFE00 - 0x8100)
        if addr not in reserved:
            return addr
    raise ProvisionError("no free mesh address")


async def associate(link, passphrase: str, device: NewDevice, address: int) -> int:
    """Run the MASP handshake; returns the address the device now has."""
    sm = _QueuedAssociation(
        client=_LinkClient(link),
        uuid_hash=device.uuid_hash,
        passphrase=passphrase,
        device_id=address,
    )
    link.set_raw_sink(sm.feed_raw)
    try:
        ok = await sm.run()
    finally:
        link.set_raw_sink(None)
    if not ok:
        detail = sm.protocol.error or sm.error or "association failed"
        raise ProvisionError(f"{detail}; device answers seen: {','.join(sm.seen) or 'none'}")
    _LOGGER.info("associated %s as %04X", device.label, sm.protocol.device_id)
    return sm.protocol.device_id


def disassociate_cmd(address: int) -> bytes:
    """MCP disassociate: the device forgets the network (factory state)."""
    return address.to_bytes(2, "little") + bytes([DISASSOCIATE_OPCODE]) + bytes(10)
