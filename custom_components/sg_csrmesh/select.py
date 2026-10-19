"""Dimmer settings with fixed choices, as select entities (Configuration).

Start-up fading, dimming speed and push-button input. The dimmer stores them
but has no command to read them back, so each shows the value Home Assistant
last sent (kept in the entry's options) and stays unknown until then. Fading
and dimming speed travel in one command, so changing one sends the other's
last value too (normal speed if never set).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SGConfigEntry
from .const import CONF_FADING, CONF_PUSH, CONF_SPEED, DOMAIN
from .csrmesh.settings import FADING_TENTHS
from .entity import SGEntity

FADING_OPTIONS = {f"fade_{t}": t for t in FADING_TENTHS}  # "fade_5" = 0.5 s
SPEED_OPTIONS = {"normal": 0, "fast": 1}
PUSH_OPTIONS = {"normal": 0, "sensor": 1}


async def async_setup_entry(
    hass: HomeAssistant, entry: SGConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[SelectEntity] = []
    for d in c.devices.values():
        if (info := d.model_info) is None:
            continue
        if info.fading:
            entities += [SGFadingSelect(c, d), SGSpeedSelect(c, d)]
        if info.push_input:
            entities.append(SGPushSelect(c, d))
    add(entities)


class _SettingSelect(SGEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _key: str
    _conf: str
    _choices: dict[str, int]

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._attr_unique_id}_{self._key}"
        self._attr_translation_key = f"sg_{self._key}"
        self._attr_options = list(self._choices)

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.device_cfg(self.device.mesh_id).get(self._conf)
        return next((k for k, v in self._choices.items() if v == value), None)

    def _not_confirmed(self) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="setting_not_confirmed",
            translation_placeholders={"name": self.device.name},
        )


class SGFadingSelect(_SettingSelect):
    _key = "fading"
    _conf = CONF_FADING
    _choices = FADING_OPTIONS

    async def async_select_option(self, option: str) -> None:
        cfg = self.coordinator.device_cfg(self.device.mesh_id)
        fast = bool(cfg.get(CONF_SPEED, 0))
        if not await self.coordinator.async_set_fading(
            self.device.mesh_id, FADING_OPTIONS[option], fast
        ):
            raise self._not_confirmed()


class SGSpeedSelect(_SettingSelect):
    _key = "dimming_speed"
    _conf = CONF_SPEED
    _choices = SPEED_OPTIONS

    async def async_select_option(self, option: str) -> None:
        cfg = self.coordinator.device_cfg(self.device.mesh_id)
        # Never set from HA: send fading off rather than guess (the app's default).
        tenths = cfg.get(CONF_FADING, 0)
        if not await self.coordinator.async_set_fading(
            self.device.mesh_id, tenths, bool(SPEED_OPTIONS[option])
        ):
            raise self._not_confirmed()


class SGPushSelect(_SettingSelect):
    _key = "push_switch"
    _conf = CONF_PUSH
    _choices = PUSH_OPTIONS

    async def async_select_option(self, option: str) -> None:
        if not await self.coordinator.async_set_push_input(
            self.device.mesh_id, bool(PUSH_OPTIONS[option])
        ):
            raise self._not_confirmed()
