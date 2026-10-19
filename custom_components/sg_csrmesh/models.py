"""SG Gen1 products: what each model is and which settings it has.

The keys are SG's own type names, the same the SG gateway reports as model
over HomeKit (and the SG app uses internally). Which settings a model has
follows the SG app's own checks (docs/gen1-protocol.md 4): trim for the Pill
Micro and the Gen3/Gen4 types, fading, dimming speed and push-button input for
Gen3/Gen4 types only. A device's model cannot be read over the mesh: it comes
from HomeKit when a device is moved from the gateway, otherwise the user picks
it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .csrmesh.protocol import DeviceKind


@dataclass(frozen=True, slots=True)
class Model:
    key: str
    product: str              # shown as the device's model in HA
    kind: DeviceKind
    trim: bool = False        # min / start-up / max level (FF29/FF2A)
    fading: bool = False      # start-up fading and dimming speed (FF48)
    push_input: bool = False  # external push-button input (FF47)
    firmware: bool = False    # firmware read (FF5E)


MODELS: dict[str, Model] = {
    m.key: m
    for m in (
        Model("DimmerGen4", "LEDDim Smart (Gen4)", DeviceKind.DIMMER,
              trim=True, fading=True, push_input=True, firmware=True),
        Model("Dimmer", "LEDDim Smart", DeviceKind.DIMMER),
        Model("MicroPillGen4", "LEDDim Smart Pill Micro (Gen4)", DeviceKind.DIMMER,
              trim=True, fading=True, push_input=True, firmware=True),
        Model("MicroPill", "LEDDim Smart Pill Micro", DeviceKind.DIMMER, trim=True),
        Model("OnOffPillMicro", "LEDDim Smart Pill On/Off Micro", DeviceKind.ONOFF),
        Model("SmartSwitchV2", "Smart Switch", DeviceKind.SWITCH),
        Model("SmartSwitchGen4", "Smart Switch (Gen4)", DeviceKind.SWITCH),
        Model("ThermostatGen3", "HeatReg Smart", DeviceKind.THERMOSTAT),
    )
}

# What the user picks when adding a device by hand, most likely first.
MODEL_CHOICES = [
    "DimmerGen4", "MicroPillGen4", "OnOffPillMicro", "SmartSwitchV2", "ThermostatGen3",
    "Dimmer", "MicroPill", "SmartSwitchGen4",
]

# Smart Switch panel: two rockers (four buttons) or one big rocker.
PANEL_TWO = "two_rockers"
PANEL_ONE = "one_rocker"
PANELS = [PANEL_TWO, PANEL_ONE]

# Rockers as the switch is configured: FF13/FF17 take the rocker's top button
# (0 left, 3 right); a big rocker presses both, so both are configured.
ROCKERS: dict[str, dict[str, tuple[int, ...]]] = {
    PANEL_TWO: {"left": (0,), "right": (3,)},
    PANEL_ONE: {"all": (0, 3)},
}

# FF15 button -> event entity key. Two rockers: one entity per button.
# One big rocker: its top presses buttons 0 and 3, its bottom 1 and 2.
BUTTON_KEYS: dict[str, dict[int, str]] = {
    PANEL_TWO: {0: "top_left", 1: "bottom_left", 2: "bottom_right", 3: "top_right"},
    PANEL_ONE: {0: "top", 3: "top", 1: "bottom", 2: "bottom"},
}


def model_for(key: str | None) -> Model | None:
    return MODELS.get(key or "")


def models_of_kind(kind: DeviceKind) -> list[str]:
    return [k for k in MODEL_CHOICES if MODELS[k].kind == kind]
