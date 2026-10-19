"""Move a device from the SG gateway (HomeKit) into this integration.

The SG gateway is paired to Home Assistant through HomeKit Controller, so its
devices already exist in HA with the user's names, rooms and SG model names,
but without their CSRmesh address. The address is found by watching the mesh
while the gateway is made to talk to the device:

* lights and thermostats: HA changes the device through HomeKit, the gateway
  sends the command into the mesh (visible to us: source 0x8000, destination =
  the device), the device answers, and HA puts the old state back. The lamp
  blinks once, or the set-point moves half a degree for a moment.
* Smart Switches cannot be commanded; the user presses a button and the
  switch's own message reveals its address.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .csrmesh.protocol import ADDR_APP, ADDR_BROADCAST, DeviceKind, Message
from .models import MODELS

_LOGGER = logging.getLogger(__name__)

HOMEKIT_DOMAIN = "homekit_controller"
SG_MANUFACTURERS = ("SG Armaturen AS", "SG Armaturen")

# SG model name (as the gateway reports it over HomeKit) -> what we create.
MODEL_KIND: dict[str, DeviceKind] = {key: m.kind for key, m in MODELS.items()}

# Gateway housekeeping that is not caused by our trigger.
GATEWAY_ROUTINE_OPS = {0xFF04, 0xFF3C, 0xFF01, 0xFF26}
# Answers to that housekeeping.
DEVICE_ROUTINE_OPS = {0xFF05, 0xFF3D, 0xFF5C, 0xFF08}

TRIGGER_WINDOW = 8.0      # s to wait for the mesh to react
SETTLE = 1.2              # s to keep collecting after the first hit
SWITCH_WINDOW = 60.0      # s the user has to press a switch button


class IdentifyError(Exception):
    """The device's mesh address could not be determined."""


@dataclass(slots=True)
class GatewayDevice:
    device_id: str
    name: str
    model: str
    area_id: str | None
    serial: str | None
    sw_version: str | None
    entity_ids: list[str]

    @property
    def kind(self) -> DeviceKind | None:
        return MODEL_KIND.get(self.model)

    def entity(self, domain: str) -> str | None:
        return next((e for e in self.entity_ids if e.startswith(f"{domain}.")), None)


def gateway_devices(hass: HomeAssistant) -> list[GatewayDevice]:
    """SG Gen1 devices the gateway exposes through HomeKit, sorted by room."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    out: list[GatewayDevice] = []
    homekit_devices = {
        dev.id: dev
        for hk in hass.config_entries.async_entries(HOMEKIT_DOMAIN)
        for dev in dr.async_entries_for_config_entry(dev_reg, hk.entry_id)
    }
    for dev in homekit_devices.values():
        if dev.manufacturer not in SG_MANUFACTURERS or dev.via_device_id is None:
            continue
        if dev.model not in MODEL_KIND:
            continue
        if dev.model == "Dimmer" and (dev.sw_version or "").startswith("1.1."):
            continue  # SG Smart 3.0 wheel/pill bridged by the gateway (Bluetooth SIG Mesh)
        entities = [
            e.entity_id
            for e in er.async_entries_for_device(ent_reg, dev.id, include_disabled_entities=True)
        ]
        out.append(
            GatewayDevice(
                dev.id,
                dev.name_by_user or dev.name or "SG",
                dev.model or "",
                dev.area_id,
                dev.serial_number,
                dev.sw_version,
                entities,
            )
        )
    out.sort(key=lambda d: (d.area_id or "~", d.name))
    return out


def is_trigger_evidence(msg: Message, exclude: set[int]) -> int | None:
    """If ``msg`` points at a device, return that device's address."""
    if msg.source == ADDR_APP:
        if msg.opcode in GATEWAY_ROUTINE_OPS:
            return None
        addr = msg.dest
    else:
        if msg.opcode in DEVICE_ROUTINE_OPS or msg.dest != ADDR_APP and msg.dest != ADDR_BROADCAST:
            return None
        addr = msg.source
    if addr in (ADDR_APP, ADDR_BROADCAST, 0x8001) or addr in exclude:
        return None
    return addr


class Watch:
    """Collect evidence from mesh traffic for a limited time."""

    def __init__(self, exclude: set[int]) -> None:
        self.exclude = exclude
        self.hits: Counter[int] = Counter()
        self.first_hit: float | None = None
        self.event = asyncio.Event()

    def __call__(self, msg: Message) -> None:
        if (addr := is_trigger_evidence(msg, self.exclude)) is None:
            return
        self.hits[addr] += 1
        if self.first_hit is None:
            self.first_hit = time.monotonic()
            self.event.set()

    async def result(self, window: float) -> int:
        try:
            await asyncio.wait_for(self.event.wait(), window)
        except TimeoutError as err:
            raise IdentifyError("no_reaction") from err
        await asyncio.sleep(SETTLE)
        (addr, _), *rest = self.hits.most_common()
        if rest and rest[0][1] == self.hits[addr]:
            raise IdentifyError("ambiguous")
        return addr


async def _call(hass: HomeAssistant, domain: str, service: str, data: dict) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def identify(
    hass: HomeAssistant, coordinator, device: GatewayDevice, exclude: set[int]
) -> int:
    """Return the mesh address of ``device`` by making the gateway talk to it."""
    watch = Watch(exclude)
    remove = coordinator.add_observer(watch)
    try:
        if device.kind == DeviceKind.SWITCH:
            return await watch.result(SWITCH_WINDOW)

        if (light := device.entity("light")) is not None:
            st = hass.states.get(light)
            if st is None or st.state == "unavailable":
                raise IdentifyError("homekit_unavailable")
            was_on = st.state == "on"
            brightness = st.attributes.get("brightness")
            if was_on:
                await _call(hass, "light", "turn_off", {"entity_id": light})
            else:
                await _call(hass, "light", "turn_on", {"entity_id": light})
            try:
                return await watch.result(TRIGGER_WINDOW)
            finally:
                await asyncio.sleep(0.8)  # long enough to see the blink
                with contextlib.suppress(Exception):
                    if was_on:
                        data = {"entity_id": light}
                        if brightness:
                            data["brightness"] = brightness
                        await _call(hass, "light", "turn_on", data)
                    else:
                        await _call(hass, "light", "turn_off", {"entity_id": light})

        if (climate := device.entity("climate")) is not None:
            st = hass.states.get(climate)
            target = st and st.attributes.get("temperature")
            if target is None:
                raise IdentifyError("homekit_unavailable")
            maxt = st.attributes.get("max_temp", 35)
            nudged = target - 0.5 if target + 0.5 > maxt else target + 0.5
            await _call(hass, "climate", "set_temperature", {"entity_id": climate, "temperature": nudged})
            try:
                return await watch.result(TRIGGER_WINDOW)
            finally:
                with contextlib.suppress(Exception):
                    await _call(
                        hass, "climate", "set_temperature", {"entity_id": climate, "temperature": target}
                    )

        raise IdentifyError("no_homekit_entity")
    finally:
        remove()


def disable_homekit_entities(hass: HomeAssistant, device: GatewayDevice) -> int:
    """Hide the gateway's duplicate of a migrated device. Returns how many."""
    ent_reg = er.async_get(hass)
    n = 0
    for entity_id in device.entity_ids:
        entry = ent_reg.async_get(entity_id)
        if entry and entry.disabled_by is None:
            ent_reg.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
            n += 1
    return n


def enable_homekit_entities(hass: HomeAssistant, device_id: str) -> None:
    """Undo ``disable_homekit_entities`` (only what the user/we disabled)."""
    ent_reg = er.async_get(hass)
    for entry in er.async_entries_for_device(ent_reg, device_id, include_disabled_entities=True):
        if entry.disabled_by == er.RegistryEntryDisabler.USER:
            ent_reg.async_update_entity(entry.entity_id, disabled_by=None)
