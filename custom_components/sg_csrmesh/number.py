"""Trim levels stored in the dimmer, as number entities (Configuration).

Minimum, start-up and maximum level, as in the SG app. The dimmer is the
record: the levels are read from it once per start, and every change is read
back. On the air they are indexes into the SG app's level tables
(csrmesh/settings.py), so a value between two steps lands on the next step.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .const import DOMAIN, SIGNAL_UPDATE
from .csrmesh.settings import MAX_LEVELS, MIN_LEVELS, TrimKind
from .entity import SGEntity

_KEYS = {TrimKind.MIN: "trim_min", TrimKind.STARTUP: "trim_start_up", TrimKind.MAX: "trim_max"}


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add(
        SGTrimNumber(c, d, kind)
        for d in c.devices.values()
        if d.model_info and d.model_info.trim
        for kind in TrimKind
    )


class SGTrimNumber(SGEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, device, kind: TrimKind) -> None:
        super().__init__(coordinator, device)
        self._kind = kind
        key = _KEYS[kind]
        self._attr_translation_key = f"sg_{key}"
        self._attr_unique_id = f"{self._attr_unique_id}_{key}"
        levels = MAX_LEVELS if kind == TrimKind.MAX else MIN_LEVELS
        self._attr_native_min_value = levels[0]
        self._attr_native_max_value = levels[-1]
        self._attr_native_step = 1 if kind == TrimKind.MAX else 0.5

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_UPDATE}_{self.device.hex_id}", self._read_if_unknown
            )
        )
        self._read_if_unknown()

    @callback
    def _read_if_unknown(self) -> None:
        # Once per start: nothing but the SG app (or this entity) changes it.
        if self.coordinator.available and self.device.trim is None:
            self.coordinator.async_request_trim(self.device.mesh_id)

    @property
    def native_value(self) -> float | None:
        if (t := self.device.trim) is None:
            return None
        return {
            TrimKind.MIN: t.min_percent,
            TrimKind.STARTUP: t.startup_percent,
            TrimKind.MAX: t.max_percent,
        }[self._kind]

    async def async_set_native_value(self, value: float) -> None:
        if not await self.coordinator.async_set_trim(self.device.mesh_id, self._kind, value):
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setting_not_confirmed",
                translation_placeholders={"name": self.device.name},
            )
