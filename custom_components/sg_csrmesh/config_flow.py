"""Config and options flow.

Setup asks only for the network passphrase (or a raw key). Devices are added
afterwards from the options menu:

* Move from SG Gateway - pick one device the gateway exposes through HomeKit;
  HA finds its mesh address by making the gateway talk to it, you confirm by
  watching it blink, and it is created with the gateway's name and room.
* Add a new device - join a factory-reset device to the network (MASP).
* Add by address - advanced: pick an address heard on the mesh or type it.
* Pair a switch - tell a Smart Switch rocker which light to control.
* Edit a device - model (which settings it gets) and Smart Switch panel.

A device's model cannot be read over the mesh: moved devices take it from
HomeKit, otherwise the user picks it. A Smart Switch also gets its panel: two
rockers (four buttons) or one big rocker.
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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_DEVICES,
    CONF_KIND,
    CONF_MESH_ID,
    CONF_MIN_LEVEL,
    CONF_MODEL,
    CONF_NETWORK_KEY,
    CONF_PAIRS,
    CONF_PANEL,
    CONF_PASSPHRASE,
    CONF_POLL_INTERVAL,
    CONF_TAG,
    DEFAULT_MIN_LEVEL,
    DEFAULT_PASSPHRASE,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    SIGNAL_PAIRINGS,
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
from .models import (
    MODEL_CHOICES,
    MODELS,
    PANEL_TWO,
    PANELS,
    ROCKERS,
    model_from_name,
    models_of_kind,
)

CONF_AREA = "area_id"
CONF_HOMEKIT = "homekit_device"
CONF_HIDE_HOMEKIT = "hide_homekit"
LIGHT_KINDS = (DeviceKind.DIMMER, DeviceKind.ONOFF)


def _model_selector(choices: list[str] = MODEL_CHOICES) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=choices, translation_key="model", mode=SelectSelectorMode.DROPDOWN)
    )


def _panel_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=PANELS, translation_key="panel", mode=SelectSelectorMode.LIST)
    )


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
        # Keep every existing option: a fixed key list silently deletes
        # anything added later (the 3.0 integration lost settings that way).
        opts = dict(self.config_entry.options)
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
                "migrate_device", "add_new", "add_by_address", "pair_switch",
                "edit_device", "remove_device", "settings",
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
        cfg = {
            "name": self._gw.name,
            CONF_KIND: self._gw.kind.value,
            CONF_AREA: self._gw.area_id,
            CONF_HOMEKIT: self._gw.device_id,
            CONF_MODEL: self._gw.model,
        }
        if self._gw.sw_version:
            cfg["sw_version"] = self._gw.sw_version
        if self._hide_homekit:
            disable_homekit_entities(self.hass, self._gw)
        return await self._save_device(f"{self._found:04x}", cfg)

    # --------------------------------------------- saving a new device

    _pending: tuple[str, dict] | None = None

    async def _save_device(self, hex_id: str, cfg: dict) -> ConfigFlowResult:
        """Store a new device; a Smart Switch first gets its panel chosen."""
        self._pending = (hex_id, cfg)
        if cfg[CONF_KIND] == DeviceKind.SWITCH and CONF_PANEL not in cfg:
            return await self.async_step_switch_panel()
        return self._store_pending()

    async def async_step_switch_panel(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending[1][CONF_PANEL] = user_input[CONF_PANEL]
            return self._store_pending()
        return self.async_show_form(
            step_id="switch_panel",
            data_schema=vol.Schema({vol.Required(CONF_PANEL, default=PANEL_TWO): _panel_selector()}),
            description_placeholders={"name": self._pending[1]["name"]},
        )

    def _store_pending(self) -> ConfigFlowResult:
        hex_id, cfg = self._pending
        opts = self._opts()
        opts[CONF_DEVICES][hex_id] = {**opts[CONF_DEVICES].get(hex_id, {}), **cfg}
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

    _unplaced: dict[int, str] | None = None

    async def _search(self) -> list:
        """Both kinds of new device, at once.

        * factory reset (no network key): association search (MASP);
        * removed from the SG gateway (key kept, no location): name query,
          as the SG app does. These are added by address, without a key
          exchange.
        """
        from .provision import discover

        coord = self._coordinator
        known = {int(k, 16) for k in self.config_entry.options.get(CONF_DEVICES, {})}
        new, unplaced = await asyncio.gather(
            discover(coord.link), coord.async_find_unplaced()
        )
        self._unplaced = {a: n for a, n in unplaced.items() if a not in known}
        return new

    async def async_step_add_new_scan(self, user_input=None) -> ConfigFlowResult:
        if self._task is None:
            self._unplaced = {}
            self._task = self.hass.async_create_task(self._search(), "sg_csrmesh discover")
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

    async def async_step_add_new_none(self, user_input=None) -> ConfigFlowResult:
        """Nothing answered the search. (HA needs a handler for every step id.)"""
        await self._unpin()
        return self.async_show_menu(
            step_id="add_new_none", menu_options=["add_new", "add_new_direct", "init"]
        )

    # A device the mesh search cannot reach is set up directly, as the SG app
    # does from a phone: the link connects to the device itself, which then
    # answers the search and the key exchange on its own connection.

    _direct_name: str | None = None
    _direct_address: str | None = None

    async def async_step_add_new_direct(self, user_input=None) -> ConfigFlowResult:
        """Pick a node seen by Bluetooth to connect to directly."""
        if self._coordinator is None:
            return self.async_abort(reason="not_loaded")
        nodes = self._coordinator.link.visible_nodes()
        if not nodes:
            return self.async_abort(reason="no_nodes")
        if user_input is not None:
            node = next((n for n in nodes if n.address == user_input["node"]), None)
            if node is not None:
                self._direct_name = node.name or node.address
                self._direct_address = node.address
                self._task = None
                return await self.async_step_add_new_direct_scan()
        options = [
            SelectOptionDict(value=n.address, label=f"{n.name or n.address} · {n.rssi} dBm")
            for n in nodes
        ]
        return self.async_show_form(
            step_id="add_new_direct",
            data_schema=vol.Schema(
                {
                    vol.Required("node"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def _direct_search(self, address: str) -> list:
        from .provision import discover

        link = self._coordinator.link
        await link.pin(address)
        return await discover(link, seconds=8.0)

    async def async_step_add_new_direct_scan(self, user_input=None) -> ConfigFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(
                self._direct_search(self._direct_address), "sg_csrmesh direct search"
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="add_new_direct_scan", progress_action="direct_scan",
                progress_task=self._task, description_placeholders={"name": self._direct_name},
            )
        try:
            self._new_list = self._task.result()
        except Exception as err:  # noqa: BLE001 - shown to the user
            self._error = str(err) or type(err).__name__
            self._task = None
            return self.async_show_progress_done(next_step_id="add_new_direct_failed")
        self._task = None
        return self.async_show_progress_done(next_step_id="add_new_pick")

    async def async_step_add_new_direct_failed(self, user_input=None) -> ConfigFlowResult:
        await self._unpin()
        return self.async_abort(
            reason="direct_failed",
            description_placeholders={"name": self._direct_name or "?", "error": self._error},
        )

    async def _unpin(self) -> None:
        if (coord := self._coordinator) is not None and coord.link.pinned:
            await coord.link.pin(None)

    @callback
    def async_remove(self) -> None:
        """The dialog was closed: never leave the link on a chosen node."""
        if (coord := self._coordinator) is not None and coord.link.pinned:
            self.hass.async_create_task(coord.link.pin(None), "sg_csrmesh unpin")

    async def async_step_add_new_pick(self, user_input=None) -> ConfigFlowResult:
        unplaced = self._unplaced or {}
        if not self._new_list and not unplaced:
            return await self.async_step_add_new_none()
        if user_input and "device" in user_input:
            model = MODELS[user_input[CONF_MODEL]]
            choice = user_input["device"]
            if choice.startswith("mesh:"):
                # Already in the mesh: no key exchange, just its address.
                self._found = int(choice.removeprefix("mesh:"), 16)
                self._new_cfg = {
                    "name": user_input["name"].strip() or unplaced.get(self._found, choice),
                    CONF_KIND: model.kind.value,
                    CONF_MODEL: model.key,
                    CONF_AREA: user_input.get(CONF_AREA),
                }
                return await self.async_step_add_new_save()
            self._new_choice = next(
                d for d in self._new_list if f"{d.uuid_hash:08x}" == choice
            )
            self._new_cfg = {
                "name": user_input["name"].strip() or self._new_choice.mac,
                CONF_KIND: model.kind.value,
                CONF_MODEL: model.key,
                CONF_AREA: user_input.get(CONF_AREA),
            }
            self._task = None
            return await self.async_step_add_new_run()
        options = [
            SelectOptionDict(value=f"mesh:{a:04x}", label=f"{n} · {a:04X}")
            for a, n in sorted(unplaced.items())
        ] + [
            SelectOptionDict(value=f"{d.uuid_hash:08x}", label=d.label) for d in self._new_list or []
        ]
        names = list(unplaced.values())
        default_model = (len(options) == 1 and names and model_from_name(names[0])) or MODEL_CHOICES[0]
        from homeassistant.helpers.selector import AreaSelector

        return self.async_show_form(
            step_id="add_new_pick",
            data_schema=vol.Schema(
                {
                    vol.Required("device", default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    ),
                    vol.Optional("name", default=""): TextSelector(),
                    vol.Required(CONF_MODEL, default=default_model): _model_selector(),
                    vol.Optional(CONF_AREA): AreaSelector(),
                }
            ),
            description_placeholders={"count": str(len(options))},
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
        await self._unpin()
        try:
            self._found = self._task.result()
        except Exception as err:  # noqa: BLE001 - shown to the user
            self._error = str(err) or type(err).__name__
            self._task = None
            return self.async_show_progress_done(next_step_id="add_new_failed")
        self._task = None
        return self.async_show_progress_done(next_step_id="add_new_save")

    async def async_step_add_new_save(self, user_input=None) -> ConfigFlowResult:
        # A device joined by HA (no gateway) uses tag 00 00 from the start.
        return await self._save_device(
            f"{self._found:04x}", {**self._new_cfg, CONF_TAG: NO_TAG.hex()}
        )

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
                model = MODELS[user_input[CONF_MODEL]]
                return await self._save_device(
                    f"{mesh_id:04x}",
                    {"name": user_input["name"], CONF_KIND: model.kind.value, CONF_MODEL: model.key},
                )

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
                    vol.Required(CONF_MODEL, default=MODEL_CHOICES[0]): _model_selector(),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------- edit device

    _edit_hex: str | None = None

    def _device_choices(self, kinds=None) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=k, label=f"{v['name']} ({k.upper()})")
            for k, v in self._opts()[CONF_DEVICES].items()
            if kinds is None or v.get(CONF_KIND) in kinds
        ]

    async def async_step_edit_device(self, user_input=None) -> ConfigFlowResult:
        """Pick a device to change its model (and a switch's panel)."""
        if not self._opts()[CONF_DEVICES]:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._edit_hex = user_input["device"]
            return await self.async_step_edit_device_options()
        return self.async_show_form(
            step_id="edit_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._device_choices(), mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_edit_device_options(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts()
        cfg = opts[CONF_DEVICES][self._edit_hex]
        kind = DeviceKind(cfg[CONF_KIND])
        choices = models_of_kind(kind)
        errors: dict[str, str] = {}
        if user_input is not None:
            panel = user_input.get(CONF_PANEL, cfg.get(CONF_PANEL, PANEL_TWO))
            if (
                kind == DeviceKind.SWITCH
                and panel != cfg.get(CONF_PANEL, PANEL_TWO)
                and cfg.get(CONF_PAIRS)
            ):
                errors[CONF_PANEL] = "panel_has_pairings"
            else:
                cfg[CONF_MODEL] = user_input[CONF_MODEL]
                if kind == DeviceKind.SWITCH:
                    cfg[CONF_PANEL] = panel
                return self.async_create_entry(data=opts)
        current = cfg.get(CONF_MODEL)
        schema = {
            vol.Required(
                CONF_MODEL, default=current if current in choices else choices[0]
            ): _model_selector(choices)
        }
        if kind == DeviceKind.SWITCH:
            schema[vol.Required(CONF_PANEL, default=cfg.get(CONF_PANEL, PANEL_TWO))] = (
                _panel_selector()
            )
        return self.async_show_form(
            step_id="edit_device_options",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"name": cfg["name"]},
        )

    # ------------------------------------------------------ pair switch

    _pair_switch: str | None = None
    _pair_rocker: str | None = None
    _pair_target: str | None = None  # None: remove the pairing

    def _switch_cfg(self) -> dict:
        return self._opts()[CONF_DEVICES][self._pair_switch]

    def _pair_lights(self) -> list[SelectOptionDict]:
        """Lights the switch can control: a device only obeys its own tag."""
        devices = self._opts()[CONF_DEVICES]
        sw_tag = devices[self._pair_switch].get(CONF_TAG)
        return [
            SelectOptionDict(value=k, label=f"{v['name']} ({k.upper()})")
            for k, v in devices.items()
            if v.get(CONF_KIND) in LIGHT_KINDS
            and (sw_tag is None or v.get(CONF_TAG) in (None, sw_tag))
        ]

    def _name_of(self, hex_id: str | None) -> str:
        if hex_id is None:
            return "–"
        cfg = self._opts()[CONF_DEVICES].get(hex_id)
        return cfg["name"] if cfg else hex_id.upper()

    async def async_step_pair_switch(self, user_input=None) -> ConfigFlowResult:
        """Pick the Smart Switch (skipped when there is only one)."""
        if self._coordinator is None:
            return self.async_abort(reason="not_loaded")
        switches = self._device_choices({DeviceKind.SWITCH})
        if not switches:
            return self.async_abort(reason="no_switches")
        if user_input is not None or len(switches) == 1:
            self._pair_switch = user_input["switch"] if user_input else switches[0]["value"]
            return await self.async_step_pair_target()
        return self.async_show_form(
            step_id="pair_switch",
            data_schema=vol.Schema(
                {
                    vol.Required("switch"): SelectSelector(
                        SelectSelectorConfig(options=switches, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_pair_target(self, user_input=None) -> ConfigFlowResult:
        """Rocker, light and whether to pair or remove (two-rocker panel)."""
        return await self._pair_target_form("pair_target", user_input)

    async def async_step_pair_target_one(self, user_input=None) -> ConfigFlowResult:
        """The same for a switch with one big rocker."""
        return await self._pair_target_form("pair_target_one", user_input)

    async def _pair_target_form(self, step_id: str, user_input) -> ConfigFlowResult:
        sw = self._switch_cfg()
        panel = sw.get(CONF_PANEL, PANEL_TWO)
        rockers = list(ROCKERS[panel])
        if step_id == "pair_target" and len(rockers) == 1:
            return await self.async_step_pair_target_one(user_input)
        pairs = sw.get(CONF_PAIRS, {})
        lights = self._pair_lights()
        if not lights and not pairs:
            return self.async_abort(
                reason="no_matching_lights", description_placeholders={"switch": sw["name"]}
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            self._pair_rocker = user_input.get("rocker", rockers[0])
            if user_input["action"] == "unpair":
                self._pair_target = None
            elif not user_input.get("target"):
                errors["target"] = "pick_light"
            else:
                self._pair_target = user_input["target"]
            if not errors:
                self._task = None
                return await self.async_step_pair_press()
        schema: dict = {
            vol.Required("action", default="pair"): SelectSelector(
                SelectSelectorConfig(
                    options=["pair", "unpair"], translation_key="pair_action",
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
        if len(rockers) > 1:
            schema[vol.Required("rocker", default=rockers[0])] = SelectSelector(
                SelectSelectorConfig(
                    options=rockers, translation_key="rocker", mode=SelectSelectorMode.LIST
                )
            )
        if lights:
            schema[vol.Optional("target")] = SelectSelector(
                SelectSelectorConfig(options=lights, mode=SelectSelectorMode.DROPDOWN)
            )
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "switch": sw["name"],
                **{r: self._name_of(pairs.get(r)) for r in ("left", "right", "all")},
            },
        )

    async def async_step_pair_press(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pair_run()
        return self.async_show_form(
            step_id="pair_press",
            data_schema=vol.Schema({}),
            description_placeholders={"switch": self._switch_cfg()["name"]},
        )

    async def async_step_pair_run(self, user_input=None) -> ConfigFlowResult:
        coord = self._coordinator
        sw = self._switch_cfg()
        if self._task is None:
            sw_id = int(self._pair_switch, 16)
            buttons = ROCKERS[sw.get(CONF_PANEL, PANEL_TWO)][self._pair_rocker]
            if self._pair_target is None:
                units = coord.unpair_steps(sw_id, buttons)
            else:
                units = coord.pair_steps(sw_id, buttons, int(self._pair_target, 16))
            self._task = self.hass.async_create_task(
                coord.async_configure_switch(sw_id, units), "sg_csrmesh configure switch"
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="pair_run",
                progress_action="pair",
                progress_task=self._task,
                description_placeholders={"switch": sw["name"]},
            )
        try:
            ok = self._task.result()
        except Exception:  # noqa: BLE001 - reported as not done
            ok = False
        self._task = None
        return self.async_show_progress_done(next_step_id="pair_done" if ok else "pair_failed")

    async def async_step_pair_done(self, user_input=None) -> ConfigFlowResult:
        """Record the pairing (HA's own record) and say what changed."""
        opts = self._opts()
        sw = opts[CONF_DEVICES][self._pair_switch]
        pairs = dict(sw.get(CONF_PAIRS, {}))
        old = pairs.get(self._pair_rocker)
        if self._pair_target is None:
            pairs.pop(self._pair_rocker, None)
        else:
            pairs[self._pair_rocker] = self._pair_target
        sw[CONF_PAIRS] = pairs
        # No reload needed: the pairing sensors read the options when told.
        self.hass.config_entries.async_update_entry(self.config_entry, options=opts)
        async_dispatcher_send(self.hass, SIGNAL_PAIRINGS)
        if self._pair_target is None:
            return self.async_abort(
                reason="unpaired",
                description_placeholders={"switch": sw["name"], "light": self._name_of(old)},
            )
        return self.async_abort(
            reason="paired",
            description_placeholders={
                "switch": sw["name"], "light": self._name_of(self._pair_target)
            },
        )

    async def async_step_pair_failed(self, user_input=None) -> ConfigFlowResult:
        return self.async_abort(
            reason="pair_failed", description_placeholders={"switch": self._switch_cfg()["name"]}
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
