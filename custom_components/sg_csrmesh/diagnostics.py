"""Diagnostics: recent decrypted frames, for mapping Switch/HeatReg formats."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SGConfigEntry
from .const import CONF_NETWORK_KEY, CONF_PASSPHRASE


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SGConfigEntry
) -> dict[str, Any]:
    c = entry.runtime_data
    return {
        "data": async_redact_data(dict(entry.data), {CONF_PASSPHRASE, CONF_NETWORK_KEY}),
        "options": dict(entry.options),
        "available": c.available,
        "bridge_node": c.link.bridge_address,
        "link": {
            "connected": c.link.connected,
            "connects": c.link.connects,
            "drops": c.link.drops,
            "last_connect_s": c.link.last_connect_s,
        },
        "commands": c.stats,
        "visible_nodes": [
            {"name": n.name, "address": n.address, "rssi": n.rssi, "source": n.source}
            for n in c.link.visible_nodes()
        ],
        "stale_nodes": [
            {"name": n.name, "address": n.address, "rssi": n.rssi, "source": n.source}
            for n in c.link.visible_nodes(fresh_only=False)
            if n.address not in {v.address for v in c.link.visible_nodes()}
        ],
        "quarantined": c.link.quarantined,
        # Seen, but only by scanners that cannot connect (passive/no free slot).
        "seen_not_connectable": [
            {"name": n.name, "address": n.address, "rssi": n.rssi, "source": n.source}
            for n in c.link.visible_nodes(connectable=False)
            if n.address not in {v.address for v in c.link.visible_nodes()}
        ],
        "devices": {
            d.hex_id: {"name": d.name, "kind": d.kind, "on": d.on, "level": d.level,
                        "tag": d.tag.hex(), "last_seen": d.last_seen}
            for d in c.devices.values()
        },
        "raw_notifications_last_capture": c.raw_count,
        "decrypted_frames_last_capture": c.decoded_count,
        # Mesh addresses heard answering (FF05/FF50), with their last body.
        "seen_addresses": {
            f"{a:04x}": {"body": s.body, "count": s.count, "last": s.last}
            for a, s in sorted(c.seen.items())
        },
        "frames": list(c.frames),
    }
