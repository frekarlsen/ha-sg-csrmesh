"""SG Smart Gen1 (CSRmesh): local control of first-generation SG devices."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICES, CONF_MODEL, DOMAIN, SERVICE_CAPTURE
from .coordinator import MeshCoordinator
from .models import model_for

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

type SGConfigEntry = ConfigEntry[MeshCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _config) -> bool:
    async def _capture(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_capture(call.data["seconds"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_CAPTURE,
        _capture,
        schema=vol.Schema({vol.Optional("seconds", default=30): vol.All(int, vol.Range(5, 600))}),
    )
    def _msgs(call: ServiceCall) -> list[bytes]:
        return [bytes.fromhex(m.replace(" ", "")) for m in call.data["messages"]]

    async def _send(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_send_raw(_msgs(call), repeats=call.data["repeats"])

    async def _send_on_press(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_send_on_press(
                int(call.data["source"], 16), _msgs(call), timeout=call.data["timeout"],
                gap=call.data["gap"], repeats=call.data["repeats"],
            )

    hex_list = vol.All(cv.ensure_list, [cv.matches_regex(r"^[0-9a-fA-F ]+$")])
    hass.services.async_register(
        DOMAIN, "send", _send,
        schema=vol.Schema({vol.Required("messages"): hex_list,
                           vol.Optional("repeats", default=2): vol.All(int, vol.Range(1, 5))}),
    )
    hass.services.async_register(
        DOMAIN, "send_on_press", _send_on_press,
        schema=vol.Schema({vol.Required("source"): cv.matches_regex(r"^[0-9a-fA-F]{4}$"),
                           vol.Required("messages"): hex_list,
                           vol.Optional("timeout", default=60): vol.All(int, vol.Range(5, 300)),
                           vol.Optional("repeats", default=2): vol.All(int, vol.Range(1, 5)),
                           vol.Optional("gap", default=0.3): vol.All(
                               vol.Coerce(float), vol.Range(0, 2))}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SGConfigEntry) -> bool:
    coordinator = MeshCoordinator(hass, entry)
    entry.runtime_data = coordinator
    # Never hard-fails: entities show unavailable until a proxy sees a node.
    await coordinator.async_start()
    _prune_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _apply_areas(hass, entry)
    _model_issues(hass, entry)
    return True


def _model_issues(hass: HomeAssistant, entry: SGConfigEntry) -> None:
    """Ask (as a repair) for the model of devices added before 0.7.0.

    The model decides which settings a device gets; it cannot be read over
    the mesh. Without one, a device still works, with no settings.
    """
    devices = entry.options.get(CONF_DEVICES, {})
    for hex_id, cfg in devices.items():
        issue_id = f"missing_model_{hex_id}"
        if model_for(cfg.get(CONF_MODEL)) is None:
            ir.async_create_issue(
                hass, DOMAIN, issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_model",
                translation_placeholders={"name": cfg.get("name", hex_id), "address": hex_id.upper()},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    # Issues for devices that were removed.
    for issue_id in [
        i for (d, i) in ir.async_get(hass).issues if d == DOMAIN and i.startswith("missing_model_")
    ]:
        if issue_id.removeprefix("missing_model_") not in devices:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _prune_devices(hass: HomeAssistant, entry: SGConfigEntry) -> None:
    """Drop registry devices (and their entities) no longer in the options.

    "Remove device" takes a device out of the options; this makes it vanish
    from HA as well instead of lingering with unavailable entities.
    """
    try:
        dev_reg = dr.async_get(hass)
        keep = set(entry.options.get(CONF_DEVICES, {}))
        for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            ids = {ident[1] for ident in dev.identifiers if ident[0] == DOMAIN}
            if ids and not ids & keep:
                _LOGGER.info("removing %s from the device registry", dev.name)
                dev_reg.async_remove_device(dev.id)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("could not prune removed devices")


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SGConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device from the UI only once it is out of the mesh list."""
    keep = set(entry.options.get(CONF_DEVICES, {}))
    return not any(i[0] == DOMAIN and i[1] in keep for i in device.identifiers)


def _apply_areas(hass: HomeAssistant, entry: SGConfigEntry) -> None:
    """Put devices moved from the gateway in the gateway's room (once).

    Never lets a registry quirk stop the integration from loading.
    """
    try:
        dev_reg = dr.async_get(hass)
        ours = {
            ident[1]: dev
            for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
            for ident in dev.identifiers
            if ident[0] == DOMAIN
        }
        for hex_id, cfg in entry.options.get(CONF_DEVICES, {}).items():
            area = cfg.get("area_id")
            dev = ours.get(hex_id)
            if area and dev and dev.area_id is None:
                dev_reg.async_update_device(dev.id, area_id=area)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("could not assign rooms")


async def async_unload_entry(hass: HomeAssistant, entry: SGConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok
