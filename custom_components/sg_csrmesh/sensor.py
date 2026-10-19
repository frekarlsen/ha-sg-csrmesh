"""Sensors: switch battery, switch pairings («Styrer» / «Styres av»), HeatReg temperatures.

The pairings are Home Assistant's own record of what it has sent: a Smart
Switch cannot be asked which lights its buttons control, so a pairing made in
the SG app does not show until it is made again from Home Assistant.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .const import CONF_DEVICES, CONF_PAIRS, SIGNAL_PAIRINGS
from .coordinator import MeshCoordinator
from .csrmesh.protocol import DeviceKind
from .entity import SGEntity, device_name

LIGHT_KINDS = (DeviceKind.DIMMER, DeviceKind.ONOFF)


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[SensorEntity] = []
    for d in c.devices.values():
        if d.kind == DeviceKind.SWITCH:
            entities += [SGBattery(c, d), SGSwitchTargets(c, d)]
        elif d.kind in LIGHT_KINDS:
            entities.append(SGPairedSwitches(c, d))
        elif d.kind == DeviceKind.THERMOSTAT:
            entities += [SGTemperature(c, d, "floor"), SGTemperature(c, d, "room")]
    add(entities)


def pairings(coordinator: MeshCoordinator) -> list[tuple[str, str, str]]:
    """(switch hex, rocker, target hex) for every pairing HA has made."""
    return [
        (sw_hex, rocker, target)
        for sw_hex, cfg in coordinator.entry.options.get(CONF_DEVICES, {}).items()
        for rocker, target in cfg.get(CONF_PAIRS, {}).items()
    ]


def _join(names: list[str]) -> str:
    text = ", ".join(dict.fromkeys(names)) if names else "–"
    return text if len(text) <= 255 else text[:252] + "..."


class SGBattery(SGEntity, RestoreSensor):
    """Battery level a Smart Switch sends with every press; restored across restarts."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._attr_unique_id}_battery"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.device.battery is None and (last := await self.async_get_last_sensor_data()):
            self._attr_native_value = last.native_value

    @property
    def available(self) -> bool:
        return True  # the last report stays true while the switch sleeps

    @property
    def native_value(self):
        return self.device.battery if self.device.battery is not None else self._attr_native_value


class _PairingSensor(SGEntity, SensorEntity):
    """Shown whatever the link's state: the record is Home Assistant's own."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PAIRINGS, self.async_write_ha_state)
        )
        # The state is device names: follow a rename on either end.
        self.async_on_remove(
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry_updated)
        )

    @callback
    def _registry_updated(self, event) -> None:
        if event.data.get("action") == "update":
            self.async_write_ha_state()

    def _name(self, hex_id: str) -> str:
        return device_name(self.hass, self.coordinator, hex_id)


class SGSwitchTargets(_PairingSensor):
    """On a switch: the lights its rockers were paired to from HA («Styrer»)."""

    _attr_translation_key = "sg_switch_targets"

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._attr_unique_id}_switch_targets"

    def _mine(self) -> dict[str, str]:
        return self.coordinator.device_cfg(self.device.mesh_id).get(CONF_PAIRS, {})

    @property
    def native_value(self) -> str:
        return _join([self._name(t) for t in self._mine().values()])

    @property
    def extra_state_attributes(self) -> dict:
        return {rocker: self._name(t) for rocker, t in self._mine().items()}


class SGPairedSwitches(_PairingSensor):
    """On a light: the switches paired to it from HA («Styres av»)."""

    _attr_translation_key = "sg_paired_switches"

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._attr_unique_id}_paired_switches"

    def _mine(self) -> list[tuple[str, str]]:
        return [
            (sw, rocker)
            for sw, rocker, target in pairings(self.coordinator)
            if target == self.device.hex_id
        ]

    @property
    def native_value(self) -> str:
        return _join([self._name(sw) for sw, _ in self._mine()])

    @property
    def extra_state_attributes(self) -> dict:
        return {"switches": [{"name": self._name(sw), "rocker": r} for sw, r in self._mine()]}


class SGTemperature(SGEntity, SensorEntity):
    """Floor or room temperature of a HeatReg Smart."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, device, which: str) -> None:
        super().__init__(coordinator, device)
        self._which = which
        self._attr_translation_key = f"{which}_temperature"
        self._attr_unique_id = f"{self._attr_unique_id}_{which}_temperature"

    @property
    def native_value(self) -> float | None:
        return self.device.extra.get(self._which)
