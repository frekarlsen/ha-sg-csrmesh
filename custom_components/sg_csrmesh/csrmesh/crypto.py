"""CSRmesh authenticated bearer: key derivation and packet framing.

Follows nkaminski/csrmesh (LGPL-3.0/GPL-3.0) and the SG notes in
fiskentnt/com.sgsmart.csrmesh. No Home Assistant imports: this module is
pure protocol and is unit-tested on its own.

Frame on the air:
    | seq (3, LE) | source (2, LE) | AES-128-OFB payload | HMAC (8) | TTL (1) |

The last byte is a hop counter, not an end marker: we send 0xFF, and relayed
frames arrive with it decremented (0xFC, 0x30, ...). It is not covered by the
MAC, so it is ignored on receive.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # cryptography >= 47 moved OFB to "decrepit"; 49 drops the old path
    from cryptography.hazmat.decrepit.ciphers.modes import OFB
except ImportError:  # pragma: no cover - older cryptography
    from cryptography.hazmat.primitives.ciphers.modes import OFB

# Source address of a node without a network id (the SG app, Homey, us).
SOURCE_APP = 0x8000
DEFAULT_TTL = 0xFF
FRAME_OVERHEAD = 3 + 2 + 8 + 1  # seq + source + mac + ttl
KEY_LEN = 16


def network_key_from_passphrase(passphrase: str | int) -> bytes:
    """Derive the 128-bit network key from the installation's passphrase/PIN.

    SHA-256 over ``passphrase + b"\\x00MCP"``, digest byte-reversed, first 16.
    A 4-digit numeric PIN is zero-padded as the SG app does.
    """
    text = str(passphrase).strip()
    if text.isdigit() and len(text) < 4:
        text = text.zfill(4)
    digest = hashlib.sha256(text.encode("utf-8") + b"\x00MCP").digest()
    return digest[::-1][:KEY_LEN]


def parse_key_hex(value: str) -> bytes:
    """Parse a raw 16-byte key given as hex (spaces/colons allowed)."""
    clean = "".join(c for c in value if c in "0123456789abcdefABCDEF")
    if len(clean) != KEY_LEN * 2:
        raise ValueError("network key must be 16 bytes (32 hex characters)")
    return bytes.fromhex(clean)


def _iv(seq: bytes, source: int) -> bytes:
    # | seq (3) | 0x00 | source (2, LE) | 0x00 * 10 |
    return struct.pack("<3sxH10x", seq, source)


def _ofb(key: bytes, iv: bytes, data: bytes) -> bytes:
    # OFB is symmetric: the same call encrypts and decrypts.
    ctx = Cipher(algorithms.AES(key), OFB(iv)).encryptor()
    return ctx.update(data) + ctx.finalize()


def _mac(key: bytes, seq: bytes, source: int, payload: bytes) -> bytes:
    pre = struct.pack("<8x3sH", seq, source) + payload
    return hmac.new(key, pre, hashlib.sha256).digest()[::-1][:8]


def random_seq() -> int:
    """A 24-bit sequence number. It must differ between frames, not increase."""
    return secrets.randbelow(0xFFFFFE) + 1


def make_packet(
    key: bytes, seq: int, data: bytes, source: int = SOURCE_APP, ttl: int = DEFAULT_TTL
) -> bytes:
    """Encrypt and authenticate one MCP message."""
    seq_b = (seq & 0xFFFFFF).to_bytes(3, "little")
    payload = _ofb(key, _iv(seq_b, source), data)
    return (
        seq_b
        + source.to_bytes(2, "little")
        + payload
        + _mac(key, seq_b, source, payload)
        + bytes([ttl & 0xFF])
    )


class DecodedFrame:
    """An inbound frame that passed the MAC check."""

    __slots__ = ("seq", "source", "payload", "ttl")

    def __init__(self, seq: int, source: int, payload: bytes, ttl: int = DEFAULT_TTL) -> None:
        self.seq = seq
        self.source = source
        self.payload = payload
        self.ttl = ttl

    def __repr__(self) -> str:
        return (
            f"DecodedFrame(seq=0x{self.seq:06x}, source=0x{self.source:04x}, "
            f"payload={self.payload.hex()})"
        )


def decrypt_packet(key: bytes, frame: bytes) -> DecodedFrame | None:
    """Verify and decrypt one complete frame. ``None`` if the MAC does not match.

    Only frames authenticated with *this* key are accepted; anything else is
    dropped rather than guessed at.
    """
    if len(frame) < FRAME_OVERHEAD:
        return None
    seq_b = frame[0:3]
    source = int.from_bytes(frame[3:5], "little")
    enc = frame[5:-9]
    mac = frame[-9:-1]
    if not hmac.compare_digest(mac, _mac(key, seq_b, source, enc)):
        return None
    return DecodedFrame(
        int.from_bytes(seq_b, "little"), source, _ofb(key, _iv(seq_b, source), enc), frame[-1]
    )


def split_for_gatt(packet: bytes, mtu_payload: int = 20) -> list[tuple[str, bytes]]:
    """CSRmesh MTL framing: (characteristic suffix, chunk) pairs.

    A packet of up to 20 bytes goes to 8004 alone; a longer one sends its
    first 20 bytes to 8003 and the rest to 8004. The bridge treats a write to
    8004 as "packet complete", so a short packet left on 8003 is never sent.
    """
    if len(packet) <= mtu_payload:
        return [("8004", packet)]
    return [("8003", packet[:mtu_payload]), ("8004", packet[mtu_payload:])]
