"""Shared entity base."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .coordinator import MeshCoordinator, MeshDevice

# Shown until the user has picked a model (devices added before 0.7.0).
KIND_PRODUCT = {
    "dimmer": "LEDDim Smart / Pill Micro",
    "onoff": "Smart Pill On/Off",
    "switch": "Smart Switch",
    "thermostat": "HeatReg Smart",
}


def device_info(coordinator: MeshCoordinator, device: MeshDevice) -> DeviceInfo:
    info = device.model_info
    out = DeviceInfo(
        identifiers={(DOMAIN, device.hex_id)},
        name=device.name,
        manufacturer="SG Armaturen",
        model=info.product if info else KIND_PRODUCT[device.kind],
        serial_number=f"mesh {device.hex_id.upper()}",
    )
    # Only what is known: an explicit None would wipe the registry's value.
    if device.model:
        out["model_id"] = device.model
    if sw := coordinator.device_cfg(device.mesh_id).get("sw_version"):
        out["sw_version"] = sw
    return out


def device_name(hass: HomeAssistant, coordinator: MeshCoordinator, hex_id: str) -> str:
    """The name the user sees for a mesh address: renames in HA win."""
    if entry := coordinator.registry_device(hex_id):
        return entry.name_by_user or entry.name or hex_id.upper()
    if dev := coordinator.devices.get(int(hex_id, 16)):
        return dev.name
    return hex_id.upper()


class SGEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: MeshCoordinator, device: MeshDevice) -> None:
        self.coordinator = coordinator
        self.device = device
        self._attr_unique_id = f"{DOMAIN}_{device.hex_id}"
        self._attr_device_info = device_info(coordinator, device)

    @property
    def available(self) -> bool:
        return self.coordinator.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_UPDATE}_{self.device.hex_id}", self.async_write_ha_state
            )
        )
