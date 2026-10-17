"""Smart Switch buttons as event entities (one per button).

A press arrives as ``FF15 <tag> <button> 01 01`` sent to 0x8000.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .const import SIGNAL_UPDATE
from .csrmesh.protocol import DeviceKind
from .entity import SGEntity

EVENT_TYPES = ["press"]
BUTTON_KEYS = ["top_left", "bottom_left", "bottom_right", "top_right"]


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add(
        SGButtonEvent(c, d, b)
        for d in c.devices.values()
        if d.kind == DeviceKind.SWITCH
        for b in range(len(BUTTON_KEYS))
    )


class SGButtonEvent(SGEntity, EventEntity):
    """One button of an SG Smart Switch. Fires "press"."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator, device, button: int) -> None:
        super().__init__(coordinator, device)
        self._button = button
        self._attr_translation_key = BUTTON_KEYS[button]
        self._attr_unique_id = f"{self._attr_unique_id}_button_{button}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self.device.hex_id}_button_{self._button}",
                self._fire,
            )
        )

    @callback
    def _fire(self, event_type: str) -> None:
        self._trigger_event(event_type, {"button": self._button})
        self.async_write_ha_state()
