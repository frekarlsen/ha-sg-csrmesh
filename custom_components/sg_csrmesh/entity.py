"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .coordinator import MeshCoordinator, MeshDevice

MODEL = {
    "dimmer": "LEDDim Smart / Pill Micro",
    "onoff": "Smart Pill On/Off",
    "switch": "Smart Switch",
    "thermostat": "HeatReg Smart",
}


class SGEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: MeshCoordinator, device: MeshDevice) -> None:
        self.coordinator = coordinator
        self.device = device
        self._attr_unique_id = f"{DOMAIN}_{device.hex_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.hex_id)},
            name=device.name,
            manufacturer="SG Armaturen",
            model=device.model or MODEL[device.kind],
            serial_number=f"mesh {device.hex_id.upper()}",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_UPDATE}_{self.device.hex_id}", self.async_write_ha_state
            )
        )
