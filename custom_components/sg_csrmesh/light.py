"""Lights: LEDDim Smart / Pill Micro (dimmable) and Smart Pill On/Off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .csrmesh.protocol import LEVEL_MAX, DeviceKind
from .entity import SGEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add(
        SGLight(c, d)
        for d in c.devices.values()
        if d.kind in (DeviceKind.DIMMER, DeviceKind.ONOFF)
    )


class SGLight(SGEntity, LightEntity):
    _attr_name = None

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        mode = ColorMode.BRIGHTNESS if device.kind == DeviceKind.DIMMER else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    # HA 1..255  <->  SG min_level..100. Keeps the slider out of the dead zone.
    def _to_level(self, brightness: int) -> int:
        lo = self.coordinator.min_level
        return round(lo + (brightness - 1) * (LEVEL_MAX - lo) / 254)

    def _to_brightness(self, level: int) -> int:
        lo = self.coordinator.min_level
        if level <= lo:
            return 1
        return round(1 + (level - lo) * 254 / (LEVEL_MAX - lo))

    @property
    def is_on(self) -> bool | None:
        return self.device.on

    @property
    def brightness(self) -> int | None:
        lvl = self.device.level
        if self.device.kind != DeviceKind.DIMMER or not self.device.on or not lvl:
            return None
        return self._to_brightness(lvl)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.device.kind == DeviceKind.DIMMER and ATTR_BRIGHTNESS in kwargs:
            level = self._to_level(kwargs[ATTR_BRIGHTNESS])
        else:
            level = 0  # "on at previous level", as the SG gateway does
        await self.coordinator.async_set(self.device.mesh_id, True, level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set(self.device.mesh_id, False)
