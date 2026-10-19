"""Settings builders/parsers (csrmesh/settings.py). Byte layouts from the SG app, unverified."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "sg_csrmesh"
sys.path.insert(0, str(ROOT))

from csrmesh import crypto, protocol  # noqa: E402
from csrmesh import settings as s  # noqa: E402


def _msg(source: int, payload_hex: str) -> protocol.Message:
    msg = protocol.parse_message(crypto.DecodedFrame(1, source, bytes.fromhex(payload_hex)))
    assert msg is not None
    return msg


def test_tables():
    assert len(s.MIN_LEVELS) == 101 and len(s.MAX_LEVELS) == 51
    assert s.MIN_LEVELS[0] == 0.5 and s.MIN_LEVELS[-1] == 40.0
    assert s.MAX_LEVELS[0] == 40.0 and s.MAX_LEVELS[-1] == 100.0
    assert list(s.MIN_LEVELS) == sorted(s.MIN_LEVELS)
    assert list(s.MAX_LEVELS) == sorted(s.MAX_LEVELS)


@pytest.mark.parametrize(
    ("kind", "percent", "index"),
    [
        (s.TrimKind.MIN, 0.5, 11),   # last of the twelve 0.5 entries
        (s.TrimKind.MIN, 10.0, 53),
        (s.TrimKind.MIN, 10.2, 54),  # between entries: the next one up
        (s.TrimKind.MIN, 40.0, 100),
        (s.TrimKind.MIN, 99.0, 100),  # above the table: the top
        (s.TrimKind.STARTUP, 30.0, 84),
        (s.TrimKind.MAX, 40.0, 0),
        (s.TrimKind.MAX, 90.0, 42),
        (s.TrimKind.MAX, 100.0, 50),
    ],
)
def test_trim_index(kind, percent, index):
    assert s.trim_index(kind, percent) == index


@pytest.mark.parametrize("kind", list(s.TrimKind))
def test_trim_roundtrip(kind):
    levels = s.MAX_LEVELS if kind == s.TrimKind.MAX else s.MIN_LEVELS
    for i, pct in enumerate(levels):
        j = s.trim_index(kind, pct)
        assert levels[j] == pct and j >= i  # duplicates map to their last index


def test_trim_percent_out_of_range():
    assert s.trim_percent(s.TrimKind.MAX, 51) is None
    assert s.trim_percent(s.TrimKind.MIN, -1) is None


def test_trim_commands():
    assert s.trim_get_cmd(0xCA67).hex() == "67caff2ae7a4"
    assert s.trim_set_cmd(0xCA67, s.TrimKind.MAX, 42).hex() == "67caff29e7a4032a"
    assert s.trim_set_cmd(0x0042, s.TrimKind.MIN, 53, tag=protocol.NO_TAG).hex() == (
        "4200ff290000" "0135"
    )
    with pytest.raises(ValueError):
        s.trim_set_cmd(1, s.TrimKind.MAX, 51)


def test_other_commands():
    assert s.fading_cmd(0xCA67, 30).hex() == "67caff48e7a41e00"
    assert s.fading_cmd(0xCA67, 5, fast=True).hex() == "67caff48e7a40501"
    assert s.push_switch_cmd(0xCA67, sensor=False).hex() == "67caff47e7a400"
    assert s.push_switch_cmd(0xCA67, sensor=True).hex() == "67caff47e7a401"
    assert s.mode_cmd(0xCA67, s.Mode.PULSE).hex() == "67caff39e7a406"
    assert s.firmware_get_cmd(0xCA67).hex() == "67caff5ee7a4"


def test_parse_trim():
    lv = s.parse_trim(_msg(0xCA67, "0080ff2be7a4" "35540032"))
    assert lv == s.TrimLevels(0x35, 0x54, 0x00)
    assert lv.min_percent == 10.0 and lv.startup_percent == 30.0 and lv.max_percent == 40.0
    assert s.parse_trim(_msg(0xCA67, "0080ff2be7a4355400")) is None  # too short
    assert s.parse_trim(_msg(0xCA67, "0080ff05e7a40164")) is None


def test_parse_ack():
    ack = s.parse_ack(_msg(0xCA67, "0080ff08e7a42903"))
    assert ack == s.Ack(s.OP_TRIM_SET, b"\x03")
    assert s.parse_ack(_msg(0xCA67, "0080ff08e7a448")).opcode == s.OP_FADING
    assert s.parse_ack(_msg(0xCA67, "0080ff05e7a40164")) is None


def test_parse_trim_real_answer():
    """Captured 17.10.2026 from 9640 (joined by HA, tag 00 00) after 4096ff2a0000."""
    msg = _msg(0x9640, "0080ff2b00003554320000")
    assert msg.tag == protocol.NO_TAG
    lv = s.parse_trim(msg)
    assert lv == s.TrimLevels(53, 84, 50)
    assert (lv.min_percent, lv.startup_percent, lv.max_percent) == (10.0, 30.0, 100.0)
    assert s.trim_get_cmd(0x9640, tag=protocol.NO_TAG).hex() == "4096ff2a0000"


def test_real_acks():
    """Captured 17.10.2026 from 9640 after FF29 03 08 and FF48 1e 00."""
    trim = s.parse_ack(_msg(0x9640, "0080ff0800002903"))
    assert trim == s.Ack(s.OP_TRIM_SET, bytes([s.TrimKind.MAX]))
    fading = s.parse_ack(_msg(0x9640, "0080ff080000481e"))
    assert fading == s.Ack(s.OP_FADING, b"\x1e")
    assert s.fading_cmd(0x9640, 30, tag=protocol.NO_TAG).hex() == "4096ff4800001e00"
    assert s.trim_set_cmd(0x9640, s.TrimKind.MAX, 8, tag=protocol.NO_TAG).hex() == "4096ff2900000308"


def test_real_firmware_and_push_switch():
    """Captured 17.10.2026 from 9640."""
    assert s.parse_firmware(_msg(0x9640, "0080ff5f000001020001020001")) == "1.2.0/1.2.0/1"
    assert s.parse_firmware(_msg(0x9640, "0080ff5f0000010200010200")) is None
    assert s.parse_ack(_msg(0x9640, "0080ff0800004700")) == s.Ack(s.OP_PUSH_SWITCH, b"\x00")
    assert s.parse_ack(_msg(0x9640, "0080ff0800004800")) == s.Ack(s.OP_FADING, b"\x00")
    assert s.fading_cmd(0x9640, 0, tag=protocol.NO_TAG).hex() == "4096ff4800000000"
    assert s.push_switch_cmd(0x9640, False, tag=protocol.NO_TAG).hex() == "4096ff47000000"


def test_switch_frames_captured():
    """Captured 17.-19.10.2026 from switch c3e4 (joined by HA, tag 00 00)."""
    tag = protocol.NO_TAG
    assert s.switch_awake_cmd(0xC3E4, True, 0x05, tag=tag).hex() == "e4c3ff1200000105"
    assert s.switch_awake_cmd(0xC3E4, False, 0x0D, tag=tag).hex() == "e4c3ff120000000d"
    assert s.parse_ack(_msg(0xC3E4, "0080ff08000012")) == s.Ack(s.OP_SWITCH_AWAKE, b"")
    assert s.switch_button_cmd(0xC3E4, 0, 0x9640, 0x0B, tag=tag).hex() == (
        "e4c3ff13000000009640000b"
    )
    bat = s.parse_battery(_msg(0xC3E4, "00008301641003"))
    assert bat == s.Battery(level=100, state=0x10, counter=3)
    assert s.parse_battery(_msg(0xC3E4, "0080ff150000000101")) is None
