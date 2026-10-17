"""Protocol tests for the pure csrmesh package (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "sg_csrmesh"
sys.path.insert(0, str(ROOT))

from csrmesh import crypto, protocol  # noqa: E402

KEY = crypto.network_key_from_passphrase("1234")


# --- crypto -------------------------------------------------------------------


def test_pin_zero_padding():
    assert crypto.network_key_from_passphrase(12) == crypto.network_key_from_passphrase("0012")


def test_parse_key_hex():
    key = crypto.parse_key_hex("00:11:22:33 44 55 66 77 88 99 aa bb cc dd ee ff")
    assert key == bytes.fromhex("00112233445566778899aabbccddeeff")
    with pytest.raises(ValueError):
        crypto.parse_key_hex("zz")


def test_roundtrip():
    msg = protocol.set_cmd(0x1A2B, True, 55, tid=7)
    frame = crypto.decrypt_packet(KEY, crypto.make_packet(KEY, 0x123456, msg))
    assert frame is not None
    assert frame.payload == msg
    assert frame.seq == 0x123456
    assert frame.source == crypto.SOURCE_APP
    assert frame.ttl == crypto.DEFAULT_TTL


def test_ttl_not_covered_by_mac():
    pkt = bytearray(crypto.make_packet(KEY, 1, b"\x00\x00\x73\x11"))
    pkt[-1] = 0x30  # a relayed frame arrives with a lower hop count
    frame = crypto.decrypt_packet(KEY, bytes(pkt))
    assert frame is not None and frame.ttl == 0x30


def test_wrong_key_rejected():
    pkt = crypto.make_packet(KEY, 1, b"\x00\x00\x73\x11")
    assert crypto.decrypt_packet(crypto.network_key_from_passphrase("9999"), pkt) is None


def test_split_for_gatt():
    short = bytes(20)
    assert crypto.split_for_gatt(short) == [("8004", short)]
    long = bytes(range(27))
    assert crypto.split_for_gatt(long) == [("8003", long[:20]), ("8004", long[20:])]


# --- outbound SG messages ----------------------------------------------------


def test_set_cmd_layout():
    # <dst LE> FF03 <tag> <pwr> <level> <tid> 00 00
    assert protocol.set_cmd(0x1A2B, True, 55, tid=7).hex() == "2b1aff03e7a4013707" + "0000"
    assert protocol.set_cmd(0x1A2B, True, 55, tid=7, tag=protocol.NO_TAG).hex() == (
        "2b1aff030000013707" + "0000"
    )


def test_set_cmd_off_and_clamp():
    assert protocol.set_cmd(0x0001, False, 80, tid=1)[6:8] == b"\x00\x00"  # off sends level 0
    assert protocol.set_cmd(0x0001, True, 250, tid=0)[7] == protocol.LEVEL_MAX
    assert protocol.set_cmd(0x0001, True, 0, tid=0)[6:8] == b"\x01\x00"  # on at previous level
    assert protocol.set_cmd(0x0001, True, 5, tid=0x1FF)[8] == 0xFF  # tid is one byte


def test_poll_cmd_layout():
    assert protocol.poll_cmd(0xCA67).hex() == "67caff04e7a4"
    assert protocol.poll_cmd(protocol.ADDR_BROADCAST, protocol.NO_TAG).hex() == "0000ff040000"


# --- inbound -----------------------------------------------------------------


def _msg(source: int, payload_hex: str) -> protocol.Message:
    frame = crypto.DecodedFrame(1, source, bytes.fromhex(payload_hex))
    msg = protocol.parse_message(frame)
    assert msg is not None
    return msg


def test_parse_message_sg_extension():
    msg = _msg(0x1A2B, "0080" "ff05" "e7a4" "0137")
    assert msg.dest == protocol.ADDR_APP
    assert msg.opcode == protocol.OP_STATUS
    assert msg.tag == b"\xe7\xa4"
    assert msg.body == b"\x01\x37"


def test_parse_message_plain_opcode():
    msg = _msg(0x1A2B, "0080" "73" "f7f2")
    assert msg.opcode == 0x73 and msg.body == b"\xf7\xf2" and msg.tag is None
    assert protocol.parse_message(crypto.DecodedFrame(1, 1, b"\x00\x80")) is None


def test_light_status_answer():
    st = protocol.parse_light_status(_msg(0x1A2B, "0080ff05e7a40137"))
    assert st == protocol.LightStatus(0x1A2B, True, 55, unsolicited=False)


def test_light_status_report_and_local():
    st = protocol.parse_light_status(_msg(0x1A2B, "0080ff50e7a4002a03"))
    assert st == protocol.LightStatus(0x1A2B, False, 42, unsolicited=True)
    # FF88 by hand counts whatever the destination
    st = protocol.parse_light_status(_msg(0x1A2B, "ff7fff880000010a"))
    assert st == protocol.LightStatus(0x1A2B, True, 10, unsolicited=True)


def test_light_status_ignores_other_traffic():
    # an answer to someone else
    assert protocol.parse_light_status(_msg(0x1A2B, "0100ff05e7a40137")) is None
    # a thermostat answer (first byte not 00/01)
    assert protocol.parse_light_status(_msg(0x1A2B, "0080ff05e7a4911020")) is None
    # level out of range
    assert protocol.parse_light_status(_msg(0x1A2B, "0080ff05e7a401ff")) is None


def test_switch_event():
    assert protocol.parse_switch_event(_msg(0xF866, "0080ff15e7a4020101")) == 2
    assert protocol.parse_switch_event(_msg(0xF866, "0080ff15e7a4070101")) is None
    assert protocol.parse_switch_event(_msg(0xF866, "0100ff15e7a4020101")) is None


def test_mesh_id_from_name():
    assert protocol.mesh_id_from_name("@ND1A2B") == 0x1A2B
    assert protocol.mesh_id_from_name("@md00ff") == 0x00FF
    assert protocol.mesh_id_from_name("SG Pill") is None
    assert protocol.is_node_name("@GO1719")
    assert not protocol.is_node_name(None)


# --- fragment reassembly -----------------------------------------------------


def test_assembler_long_frame():
    msg = bytes.fromhex("0080ff05e7a4012a" "00000000")
    pkt = crypto.make_packet(KEY, 99, msg, source=0x1A2B)
    assert len(pkt) > 20
    asm = protocol.FrameAssembler(KEY)
    out = []
    for char, part in crypto.split_for_gatt(pkt):
        out += asm.feed(char, part)
    assert len(out) == 1
    assert out[0].source == 0x1A2B and out[0].payload == msg


def test_assembler_short_frame_alone():
    pkt = crypto.make_packet(KEY, 5, bytes.fromhex("0080ff05"), source=0x0042)
    assert len(pkt) <= 20
    out = protocol.FrameAssembler(KEY).feed("8004", pkt)
    assert len(out) == 1 and out[0].source == 0x0042


def test_assembler_drops_orphan_head():
    asm = protocol.FrameAssembler(KEY)
    asm.feed("8003", bytes(20))  # head whose tail never comes
    pkt = crypto.make_packet(KEY, 6, bytes.fromhex("0080ff05e7a4012a00000000"), source=7)
    out = []
    for char, part in crypto.split_for_gatt(pkt):
        out += asm.feed(char, part)
    assert [f.source for f in out] == [7]


# --- cross-check against the reference implementation ------------------------
REF = Path("/tmp/csrmesh/csrmesh/crypto.py")


@pytest.mark.skipif(
    not REF.exists() or importlib.util.find_spec("Cryptodome") is None,
    reason="nkaminski/csrmesh checkout or pycryptodomex not available",
)
@pytest.mark.parametrize("pin", ["1234", "0000", "4711"])
def test_matches_nkaminski(pin):
    spec = importlib.util.spec_from_file_location("ref_crypto", REF)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    key = crypto.network_key_from_passphrase(pin)
    assert key == ref.network_key_from_pin(pin)
    msg = protocol.set_cmd(0x0042, True, 80, tid=3)
    assert crypto.make_packet(key, 0xABCDEF, msg) == ref.make_packet(key, 0xABCDEF, msg)
