"""HeatReg Smart thermostats as climate entities (protocol: csrmesh/heatreg.py).

The current temperature is the floor sensor's, as the SG gateway shows it
(room temperature when no floor value is reported). Both are also sensors.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .const import DOMAIN
from .csrmesh.heatreg import TEMP_MAX, TEMP_MIN
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
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = TEMP_MIN
    _attr_max_temp = TEMP_MAX
    _attr_target_temperature_step = 0.5

    @property
    def current_temperature(self) -> float | None:
        extra = self.device.extra
        return extra.get("floor") if extra.get("floor") is not None else extra.get("room")

    @property
    def target_temperature(self) -> float | None:
        return self.device.extra.get("target")

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self.device.on is None:
            return None
        return HVACMode.HEAT if self.device.on else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.device.on is None:
            return None
        if not self.device.on:
            return HVACAction.OFF
        return HVACAction.HEATING if self.device.extra.get("heating") else HVACAction.IDLE

    def _not_confirmed(self) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="setting_not_confirmed",
            translation_placeholders={"name": self.device.name},
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            await self.async_set_hvac_mode(mode)
        if (target := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if not await self.coordinator.async_set_thermostat_target(self.device.mesh_id, target):
            raise self._not_confirmed()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        on = hvac_mode != HVACMode.OFF
        if not await self.coordinator.async_set_thermostat_power(self.device.mesh_id, on):
            raise self._not_confirmed()
        self.device.on = on
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
