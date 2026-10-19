"""Helpers for tests against a real HA core."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sg_csrmesh.const import DOMAIN
from custom_components.sg_csrmesh.csrmesh import crypto

KEY = crypto.network_key_from_passphrase("1234")


def make_entry(devices=None, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"passphrase": "1234"},
        options={"devices": devices or {}, "poll_interval": 0, "min_level": 10, **options},
    )


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def sent_payloads(send: AsyncMock) -> list[bytes]:
    out = []
    for call in send.await_args_list:
        frame = crypto.decrypt_packet(KEY, call.args[0])
        assert frame is not None
        out.append(frame.payload)
    return out


def inject(coordinator, source: int, payload_hex: str, seq: int = 1) -> None:
    """A frame from ``source`` arriving on the link."""
    packet = crypto.make_packet(KEY, seq, bytes.fromhex(payload_hex), source=source)
    coordinator._on_frame(crypto.decrypt_packet(KEY, packet))


def answering(coordinator, reply):
    """A send side effect: ``reply(payload) -> [(source, payload_hex), ...]``."""
    seq = [100]

    async def side_effect(packet, **_kw):
        payload = crypto.decrypt_packet(KEY, packet).payload
        for source, answer in reply(payload) or ():
            seq[0] += 1
            inject(coordinator, source, answer, seq[0])

    return side_effect
