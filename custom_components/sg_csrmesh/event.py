"""Smart Switch buttons as event entities.

A press arrives as ``FF15 <tag> <button> ..`` sent to 0x8000. With two
rockers each of the four buttons is an entity; with one big rocker its top
(buttons 0 and 3) and bottom (1 and 2) are.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .const import DOMAIN, SIGNAL_UPDATE
from .csrmesh.protocol import DeviceKind
from .entity import SGEntity
from .models import BUTTON_KEYS, PANEL_TWO

EVENT_TYPES = ["press"]


def _keys(panel: str) -> list[str]:
    """Entity keys for a panel, in display order."""
    return list(dict.fromkeys(BUTTON_KEYS.get(panel, BUTTON_KEYS[PANEL_TWO]).values()))


def _unique_suffix(panel: str, key: str) -> str:
    # Two rockers keep the ids of 0.6 (button number); the big rocker uses names.
    if panel == PANEL_TWO:
        return str(next(b for b, k in BUTTON_KEYS[PANEL_TWO].items() if k == key))
    return key


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities = [
        SGButtonEvent(c, d, key, _unique_suffix(d.panel, key))
        for d in c.devices.values()
        if d.kind == DeviceKind.SWITCH
        for key in _keys(d.panel)
    ]
    _remove_stale(hass, entry, {e.unique_id for e in entities})
    add(entities)


def _remove_stale(hass: HomeAssistant, entry: SGConfigEntry, keep: set[str]) -> None:
    """Drop button entities a panel change made obsolete."""
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.domain == "event" and ent.unique_id.startswith(f"{DOMAIN}_") and (
            "_button_" in ent.unique_id and ent.unique_id not in keep
        ):
            ent_reg.async_remove(ent.entity_id)


class SGButtonEvent(SGEntity, EventEntity):
    """One button of an SG Smart Switch. Fires "press"."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator, device, key: str, suffix: str) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{self._attr_unique_id}_button_{suffix}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self.device.hex_id}_button_{self._key}",
                self._fire,
            )
        )

    @callback
    def _fire(self, event_type: str) -> None:
        self._trigger_event(event_type, {"button": self._key})
        self.async_write_ha_state()
