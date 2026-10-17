"""HeatReg Smart thermostats as climate entities.

Read path and write path are both TODO until the frame format has been mapped
from captures. Until then the entity shows current/target as unknown and
refuses to set a temperature rather than sending a guessed command.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .csrmesh.protocol import DeviceKind
from .entity import SGEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add(SGThermostat(c, d) for d in c.devices.values() if d.kind == DeviceKind.THERMOSTAT)


class SGThermostat(SGEntity, ClimateEntity):
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = 5
    _attr_max_temp = 35
    _attr_target_temperature_step = 0.5

    @property
    def current_temperature(self) -> float | None:
        return self.device.extra.get("current")

    @property
    def target_temperature(self) -> float | None:
        return self.device.extra.get("target")

    @property
    def hvac_mode(self) -> HVACMode | None:
        return self.device.extra.get("mode")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        # TODO: build the HeatReg set-point frame once it is mapped.
        raise HomeAssistantError(
            f"HeatReg set-point is not implemented yet (wanted {kwargs.get(ATTR_TEMPERATURE)} °C)"
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        raise HomeAssistantError("HeatReg mode change is not implemented yet")
