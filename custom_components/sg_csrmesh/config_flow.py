"""Config and options flow.

Setup asks only for the network passphrase (or a raw key). Devices are added
afterwards from the options menu:

* Move from SG Gateway - pick one device the gateway exposes through HomeKit;
  HA finds its mesh address by making the gateway talk to it, you confirm by
  watching it blink, and it is created with the gateway's name and room.
* Add by address - advanced: pick an address heard on the mesh or type it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_DEVICES,
    CONF_KIND,
    CONF_MESH_ID,
    CONF_MIN_LEVEL,
    CONF_NETWORK_KEY,
    CONF_PASSPHRASE,
    CONF_POLL_INTERVAL,
    CONF_TAG,
    DEFAULT_MIN_LEVEL,
    DEFAULT_PASSPHRASE,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .csrmesh.crypto import parse_key_hex
from .csrmesh.protocol import LEVEL_MAX, NO_TAG, DeviceKind
from .migrate import (
    GatewayDevice,
    IdentifyError,
    disable_homekit_entities,
    enable_homekit_entities,
    gateway_devices,
    identify,
)

CONF_AREA = "area_id"
CONF_HOMEKIT = "homekit_device"
CONF_HIDE_HOMEKIT = "hide_homekit"
KNOWN_OPTIONS = (CONF_DEVICES, CONF_POLL_INTERVAL, CONF_MIN_LEVEL)


def _describe(addr: int, body_hex: str) -> str:
    """Dropdown label for an address heard on the mesh."""
    body = bytes.fromhex(body_hex)
    if len(body) >= 2 and body[0] in (0, 1) and body[1] <= LEVEL_MAX:
        state = f"on {body[1]} %" if body[0] else f"off (level {body[1]} %)"
    else:
        state = f"answer {body_hex}"
    return f"{addr:04X} — {state}"


class SGConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        # One entry per installation; every node is on the same network key.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            if key := user_input.get(CONF_NETWORK_KEY, "").strip():
                try:
                    parse_key_hex(key)
                except ValueError:
                    errors[CONF_NETWORK_KEY] = "invalid_key"
            if not errors:
                return self.async_create_entry(
                    title="SG Smart Gen1",
                    data=user_input,
                    options={
                        CONF_DEVICES: {},
                        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                        CONF_MIN_LEVEL: DEFAULT_MIN_LEVEL,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSPHRASE, default=DEFAULT_PASSPHRASE): str,
                    vol.Optional(CONF_NETWORK_KEY, default=""): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> SGOptionsFlow:
        return SGOptionsFlow()


class SGOptionsFlow(OptionsFlowWithReload):
    _gw: GatewayDevice | None = None
    _gw_list: list[GatewayDevice] | None = None
    _hide_homekit = True
    _task: asyncio.Task | None = None
    _found: int | None = None
    _error = ""

    def _opts(self) -> dict[str, Any]:
        opts = {k: v for k, v in self.config_entry.options.items() if k in KNOWN_OPTIONS}
        opts[CONF_DEVICES] = {
            k: dict(v) for k, v in self.config_entry.options.get(CONF_DEVICES, {}).items()
        }
        return opts

    @property
    def _coordinator(self):
        return getattr(self.config_entry, "runtime_data", None)

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "migrate_device", "add_new", "add_by_address", "remove_device", "settings",
            ],
        )

    # ------------------------------------------------- move from gateway

    def _area_name(self, area_id: str | None) -> str:
        if area_id and (area := ar.async_get(self.hass).async_get_area(area_id)):
            return area.name
        return "—"

    async def async_step_migrate_device(self, user_input=None) -> ConfigFlowResult:
        """Pick one device the SG gateway still owns."""
        if self._coordinator is None:
            return self.async_abort(reason="not_loaded")
        done = {
            cfg.get(CONF_HOMEKIT)
            for cfg in self.config_entry.options.get(CONF_DEVICES, {}).values()
        }
        self._gw_list = [d for d in gateway_devices(self.hass) if d.device_id not in done]
        if not self._gw_list:
            return self.async_abort(reason="nothing_to_migrate")
        if user_input is not None:
            self._gw = next(d for d in self._gw_list if d.device_id == user_input["device"])
            self._hide_homekit = user_input[CONF_HIDE_HOMEKIT]
            self._task = None
            if self._gw.kind == DeviceKind.SWITCH:
                return await self.async_step_migrate_press()
            return await self.async_step_migrate_run()

        options = [
            SelectOptionDict(
                value=d.device_id,
                label=f"{d.name} · {self._area_name(d.area_id)} · {d.model}",
            )
            for d in self._gw_list
        ]
        return self.async_show_form(
            step_id="migrate_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Required(CONF_HIDE_HOMEKIT, default=True): BooleanSelector(),
                }
            ),
            description_placeholders={"count": str(len(self._gw_list))},
        )

    async def async_step_migrate_press(self, user_input=None) -> ConfigFlowResult:
        """Smart Switch: tell the user to press a button before we listen."""
        if user_input is not None:
            return await self.async_step_migrate_run()
        return self.async_show_form(
            step_id="migrate_press",
            data_schema=vol.Schema({}),
            description_placeholders={"name": self._gw.name},
        )

    def _taken(self) -> set[int]:
        """Addresses already moved from the gateway (manual ones may be upgraded)."""
        return {
            int(k, 16)
            for k, cfg in self.config_entry.options.get(CONF_DEVICES, {}).items()
            if cfg.get(CONF_HOMEKIT)
        }

    async def async_step_migrate_run(self, user_input=None) -> ConfigFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(
                identify(self.hass, self._coordinator, self._gw, self._taken()),
                "sg_csrmesh identify",
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="migrate_run",
                progress_action="switch" if self._gw.kind == DeviceKind.SWITCH else "identify",
                progress_task=self._task,
                description_placeholders={"name": self._gw.name},
            )
        try:
            self._found = self._task.result()
        except IdentifyError as err:
            self._error = str(err)
            self._task = None
            return self.async_show_progress_done(next_step_id="migrate_failed")
        except Exception as err:  # noqa: BLE001 - shown to the user
            self._error = f"{type(err).__name__}: {err}"
            self._task = None
            return self.async_show_progress_done(next_step_id="migrate_failed")
        self._task = None
        return self.async_show_progress_done(next_step_id="migrate_confirm")

    async def async_step_migrate_confirm(self, user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="migrate_confirm",
            menu_options=["migrate_save", "migrate_blink", "migrate_device"],
            description_placeholders={
                "name": self._gw.name,
                "address": f"{self._found:04X}",
                "model": self._gw.model,
            },
        )

    async def async_step_migrate_blink(self, user_input=None) -> ConfigFlowResult:
        """Flash the found address from HA itself (not via the gateway)."""
        if self._gw.kind in (DeviceKind.DIMMER, DeviceKind.ONOFF):
            with contextlib.suppress(Exception):  # blinking is best effort
                await self._coordinator.async_blink(self._found)
        return await self.async_step_migrate_confirm()

    async def async_step_migrate_save(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts()
        opts[CONF_DEVICES][f"{self._found:04x}"] = {
            "name": self._gw.name,
            CONF_KIND: self._gw.kind.value,
            CONF_AREA: self._gw.area_id,
            CONF_HOMEKIT: self._gw.device_id,
            "model": self._gw.model,
        }
        if self._hide_homekit:
            disable_homekit_entities(self.hass, self._gw)
        return self.async_create_entry(data=opts)

    async def async_step_migrate_failed(self, user_input=None) -> ConfigFlowResult:
        return self.async_abort(
            reason=f"identify_{self._error}"
            if self._error in ("no_reaction", "ambiguous", "homekit_unavailable", "no_homekit_entity")
            else "identify_error",
            description_placeholders={"name": self._gw.name, "error": self._error},
        )

    # ------------------------------------------------------ add new device

    _new_list: list | None = None
    _new_choice = None
    _new_cfg: dict | None = None

    async def async_step_add_new(self, user_input=None) -> ConfigFlowResult:
        """Explain, then search for factory-reset devices."""
        if self._coordinator is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            self._task = None
            return await self.async_step_add_new_scan()
        return self.async_show_form(step_id="add_new", data_schema=vol.Schema({}))

    async def async_step_add_new_scan(self, user_input=None) -> ConfigFlowResult:
        from .provision import discover

        if self._task is None:
            self._task = self.hass.async_create_task(
                discover(self._coordinator.link), "sg_csrmesh discover"
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="add_new_scan", progress_action="scan", progress_task=self._task
            )
        try:
            self._new_list = self._task.result()
        except Exception as err:  # noqa: BLE001
            self._error = f"{type(err).__name__}: {err}"
            self._new_list = []
        self._task = None
        return self.async_show_progress_done(next_step_id="add_new_pick")

    async def async_step_add_new_pick(self, user_input=None) -> ConfigFlowResult:
        if not self._new_list:
            return self.async_show_menu(step_id="add_new_none", menu_options=["add_new", "init"])
        if user_input and "device" in user_input:
            self._new_choice = next(
                d for d in self._new_list if f"{d.uuid_hash:08x}" == user_input["device"]
            )
            self._new_cfg = {
                "name": user_input["name"].strip() or self._new_choice.mac,
                CONF_KIND: user_input[CONF_KIND],
                CONF_AREA: user_input.get(CONF_AREA),
            }
            self._task = None
            return await self.async_step_add_new_run()
        options = [
            SelectOptionDict(value=f"{d.uuid_hash:08x}", label=d.label) for d in self._new_list
        ]
        from homeassistant.helpers.selector import AreaSelector

        return self.async_show_form(
            step_id="add_new_pick",
            data_schema=vol.Schema(
                {
                    vol.Required("device", default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    ),
                    vol.Required("name"): TextSelector(),
                    vol.Required(CONF_KIND, default=DeviceKind.DIMMER): SelectSelector(
                        SelectSelectorConfig(
                            options=[k.value for k in DeviceKind], translation_key="kind"
                        )
                    ),
                    vol.Optional(CONF_AREA): AreaSelector(),
                }
            ),
            description_placeholders={"count": str(len(self._new_list))},
        )

    async def async_step_add_new_run(self, user_input=None) -> ConfigFlowResult:
        from .provision import associate, pick_address

        if self._task is None:
            coord = self._coordinator
            used = set(coord.seen) | {int(k, 16) for k in self.config_entry.options.get(CONF_DEVICES, {})}
            self._found = pick_address(used)
            self._task = self.hass.async_create_task(
                associate(coord.link, coord.passphrase, self._new_choice, self._found),
                "sg_csrmesh associate",
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="add_new_run",
                progress_action="associate",
                progress_task=self._task,
                description_placeholders={"name": self._new_cfg["name"]},
            )
        try:
            self._found = self._task.result()
        except Exception as err:  # noqa: BLE001 - shown to the user
            self._error = str(err) or type(err).__name__
            self._task = None
            return self.async_show_progress_done(next_step_id="add_new_failed")
        self._task = None
        return self.async_show_progress_done(next_step_id="add_new_save")

    async def async_step_add_new_save(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts()
        # A device joined by HA (no gateway) uses tag 00 00 from the start.
        opts[CONF_DEVICES][f"{self._found:04x}"] = {**self._new_cfg, CONF_TAG: NO_TAG.hex()}
        return self.async_create_entry(data=opts)

    async def async_step_add_new_failed(self, user_input=None) -> ConfigFlowResult:
        return self.async_abort(
            reason="add_new_failed",
            description_placeholders={"name": self._new_cfg["name"], "error": self._error},
        )

    # ---------------------------------------------------- add by address

    async def async_step_add_by_address(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts()
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input[CONF_MESH_ID].strip().split()[0]
            try:
                mesh_id = int(raw.removeprefix("0x"), 16)
            except ValueError:
                mesh_id = None
            # Real addresses seen on SG Gen1 span the full 16-bit range
            # (e.g. 0xCA67); only 0x0000 (broadcast) and 0x8000 (app) are out.
            if mesh_id is None or mesh_id in (0x0000, 0x8000) or mesh_id > 0xFFFF:
                errors[CONF_MESH_ID] = "invalid_mesh_id"
            elif f"{mesh_id:04x}" in opts[CONF_DEVICES]:
                errors[CONF_MESH_ID] = "already_added"
            else:
                opts[CONF_DEVICES][f"{mesh_id:04x}"] = {
                    "name": user_input["name"],
                    CONF_KIND: user_input[CONF_KIND],
                }
                return self.async_create_entry(data=opts)

        seen = [
            {"value": f"{addr:04x}", "label": _describe(addr, info.body)}
            for addr, info in sorted(
                (self._coordinator.seen if self._coordinator else {}).items(),
                key=lambda kv: -kv[1].last,
            )
            if f"{addr:04x}" not in opts[CONF_DEVICES]
        ]
        return self.async_show_form(
            step_id="add_by_address",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MESH_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=seen, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Required("name"): TextSelector(),
                    vol.Required(CONF_KIND, default=DeviceKind.DIMMER): SelectSelector(
                        SelectSelectorConfig(
                            options=[k.value for k in DeviceKind], translation_key="kind"
                        )
                    ),
                }
            ),
            errors=errors,
        )

    _remove_hex: str | None = None
    _remove_reset = False

    async def async_step_remove_device(self, user_input=None) -> ConfigFlowResult:
        """Pick one device; optionally factory reset it in the mesh too."""
        opts = self._opts()
        if not opts[CONF_DEVICES]:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._remove_hex = user_input["device"]
            self._remove_reset = user_input["factory_reset"]
            self._task = None
            if not self._remove_reset:
                return self._finish_remove()
            if opts[CONF_DEVICES][self._remove_hex].get(CONF_KIND) == DeviceKind.SWITCH:
                return await self.async_step_remove_press()
            return await self.async_step_remove_run()
        choices = [
            SelectOptionDict(value=k, label=f"{v['name']} ({k.upper()})")
            for k, v in opts[CONF_DEVICES].items()
        ]
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Required("factory_reset", default=False): BooleanSelector(),
                }
            ),
        )

    async def async_step_remove_press(self, user_input=None) -> ConfigFlowResult:
        name = self._opts()[CONF_DEVICES][self._remove_hex]["name"]
        if user_input is not None:
            return await self.async_step_remove_run()
        return self.async_show_form(
            step_id="remove_press", data_schema=vol.Schema({}),
            description_placeholders={"name": name},
        )

    async def async_step_remove_run(self, user_input=None) -> ConfigFlowResult:
        cfg = self._opts()[CONF_DEVICES][self._remove_hex]
        if self._task is None:
            self._task = self.hass.async_create_task(
                self._coordinator.async_factory_reset(
                    int(self._remove_hex, 16), wait_press=cfg.get(CONF_KIND) == DeviceKind.SWITCH
                ),
                "sg_csrmesh factory reset",
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="remove_run", progress_action="reset", progress_task=self._task,
                description_placeholders={"name": cfg["name"]},
            )
        from .coordinator import StillInMeshError

        self._error = "no_press"
        try:
            ok = self._task.result()
        except StillInMeshError:
            ok, self._error = False, "still_in_mesh"
        except Exception:  # noqa: BLE001
            ok = False
        self._task = None
        return self.async_show_progress_done(
            next_step_id="remove_done" if ok else "remove_failed"
        )

    async def async_step_remove_done(self, user_input=None) -> ConfigFlowResult:
        return self._finish_remove()

    async def async_step_remove_failed(self, user_input=None) -> ConfigFlowResult:
        name = self._opts()[CONF_DEVICES][self._remove_hex]["name"]
        reason = "reset_still_in_mesh" if self._error == "still_in_mesh" else "reset_no_press"
        return self.async_abort(reason=reason, description_placeholders={"name": name})

    def _finish_remove(self) -> ConfigFlowResult:
        opts = self._opts()
        cfg = opts[CONF_DEVICES].pop(self._remove_hex, None) or {}
        # Hand a migrated device back to the gateway's HomeKit entities.
        if (hk := cfg.get(CONF_HOMEKIT)) and not self._remove_reset:
            enable_homekit_entities(self.hass, hk)
        return self.async_create_entry(data=opts)

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts()
        if user_input is not None:
            opts[CONF_POLL_INTERVAL] = int(user_input[CONF_POLL_INTERVAL])
            opts[CONF_MIN_LEVEL] = int(user_input[CONF_MIN_LEVEL])
            return self.async_create_entry(data=opts)
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL, default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=3600, step=30, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        CONF_MIN_LEVEL, default=opts.get(CONF_MIN_LEVEL, DEFAULT_MIN_LEVEL)
                    ): NumberSelector(NumberSelectorConfig(min=1, max=50, step=1, unit_of_measurement="%")),
                }
            ),
        )
