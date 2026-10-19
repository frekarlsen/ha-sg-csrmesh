"""Coordinator: owns the mesh link, device list, state and the status poll."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .bridge import MeshLink, NoMeshNodeError
from .const import (
    CAPTURE_BUFFER,
    CONF_DEVICES,
    CONF_FADING,
    CONF_KIND,
    CONF_MIN_LEVEL,
    CONF_MODEL,
    CONF_NETWORK_KEY,
    CONF_PANEL,
    CONF_PASSPHRASE,
    CONF_POLL_INTERVAL,
    CONF_PUSH,
    CONF_SPEED,
    CONF_TAG,
    DEFAULT_MIN_LEVEL,
    DEFAULT_PASSPHRASE,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_FRAME,
    FIRMWARE_DELAY,
    SIGNAL_UPDATE,
    STATUS_LISTEN_TIME,
    THERMOSTAT_POLL,
)
from .csrmesh.crypto import (
    DecodedFrame,
    decrypt_packet,
    network_key_from_passphrase,
    parse_key_hex,
)
from .csrmesh.heatreg import (
    OP_SETPOINT,
    parse_status,
    parse_temperatures,
    power_cmd,
    setpoint_cmd,
    temperatures_get_cmd,
)
from .csrmesh.protocol import (
    ADDR_APP,
    ADDR_BROADCAST,
    NO_TAG,
    OWN_TAG_DESTS,
    SG_TAG,
    STATUS_OPS,
    DeviceKind,
    Message,
    parse_light_status,
    parse_message,
    parse_switch_event,
    poll_cmd,
    seal,
    set_cmd,
)
from .csrmesh.settings import (
    OP_ACK,
    OP_FADING,
    OP_FIRMWARE,
    OP_PUSH_SWITCH,
    OP_SWITCH_BUTTON,
    OP_SWITCH_CLEAR,
    OP_TRIM,
    OP_TRIM_SET,
    TrimKind,
    TrimLevels,
    fading_cmd,
    firmware_get_cmd,
    parse_ack,
    parse_battery,
    parse_firmware,
    parse_trim,
    push_switch_cmd,
    switch_awake_cmd,
    switch_button_cmd,
    switch_clear_cmd,
    trim_get_cmd,
    trim_index,
    trim_set_cmd,
)
from .models import BUTTON_KEYS, PANEL_TWO, Model, model_for

_LOGGER = logging.getLogger(__name__)

PRESS_DEDUP = 3.0  # s; copies of one press arrive within this
REPEAT_MIN, REPEAT_MAX = 32.0, 40.0  # s; a switch's own periodic repeat
POLL_SPACING = 0.3  # s between per-device polls, like the gateway

# Mesh flooding has no delivery guarantee. The SG app repeats each command
# ~3x at 300 ms; we do the same, then wait for the device's FF50 report. A
# device joined by HA never reports a change made by a command, so without a
# report we ask (FF04) and only send again if the answer shows the command
# did not take. Anything the device reports by itself (FF50/FF88) meanwhile
# means someone used it by hand: that always wins, HA stops sending.
REPEATS = 3
REPEAT_GAP = 0.25     # s
CONFIRM_TIMEOUT = 1.0  # s after the last repeat, for an FF50 report
POLL_WAIT = 2.0        # s for the FF05 answer to our check
MAX_ROUNDS = 3
RESYNC_DELAY = 1.5     # s; re-read a device after it was used by hand
RESET_SETTLE = 2.0     # s after disassociate before checking it is gone
REQUEST_WAIT = 1.5       # s for an answer before a command is sent again
SWITCH_ACK_SETTLE = 1.0  # s after a switch configuration for the last acks
SWITCH_TIMEOUT = 120.0   # s the user has to press the switch while configuring it


class StillInMeshError(HomeAssistantError):
    """A device kept answering on the mesh after it was told to leave it."""


@dataclass(slots=True)
class Delivery:
    """One HA command in flight, and what the device said meanwhile."""

    on: bool
    level: int
    event: asyncio.Event = field(default_factory=asyncio.Event)
    confirmed: bool = False
    overridden: bool = False  # the device changed by hand: HA backs off
    answered: bool = False    # an FF05 answer arrived (matching or not)

    def matches(self, on: bool, level: int) -> bool:
        return on == self.on and (not self.on or not self.level or level == self.level)


@dataclass(slots=True)
class MeshDevice:
    mesh_id: int
    name: str
    kind: DeviceKind
    on: bool | None = None            # None = unknown
    level: int | None = None          # percent; kept while off
    last_seen: float | None = None
    extra: dict = field(default_factory=dict)  # thermostat values etc.
    model: str | None = None  # SG type name (models.py); None = not chosen yet
    tag: bytes = SG_TAG  # learned from the device's own messages, persisted
    tag_known: bool = False  # stored or heard; until then polls try both
    panel: str = PANEL_TWO  # Smart Switch: two rockers or one big rocker
    trim: TrimLevels | None = None  # read from the dimmer
    battery: int | None = None  # percent, sent by a switch with every press

    @property
    def hex_id(self) -> str:
        return f"{self.mesh_id:04x}"

    @property
    def model_info(self) -> Model | None:
        return model_for(self.model)


@dataclass(slots=True)
class SwitchStep:
    """One command to a battery switch and the acknowledgement that proves it."""

    build: Callable[[int], bytes]   # transaction id -> message
    match: Callable[[Message], bool]


def ack_from(mesh_id: int, opcode: int, first: int | None = None) -> Callable[[Message], bool]:
    """Match FF08 from ``mesh_id`` acknowledging ``opcode`` (and its first data byte)."""

    def match(msg: Message) -> bool:
        if msg.source != mesh_id or msg.opcode != OP_ACK:
            return False
        ack = parse_ack(msg)
        return (
            ack is not None
            and ack.opcode == opcode
            and (first is None or ack.rest[:1] == bytes([first]))
        )

    return match


@dataclass(slots=True)
class SeenAddress:
    """A mesh address heard answering on the network (for the add-device list)."""

    body: str
    last: float
    count: int = 1


def _tag_from_cfg(cfg: dict) -> bytes:
    try:
        tag = bytes.fromhex(cfg.get(CONF_TAG, ""))
    except ValueError:
        tag = b""
    return tag if len(tag) == 2 else SG_TAG


def key_from_entry(entry: ConfigEntry) -> bytes:
    if raw := entry.data.get(CONF_NETWORK_KEY):
        return parse_key_hex(raw)
    return network_key_from_passphrase(entry.data.get(CONF_PASSPHRASE, DEFAULT_PASSPHRASE))


class MeshCoordinator:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.key = key_from_entry(entry)
        # recsrmesh derives the key it hands a new device from the passphrase.
        self.passphrase: str = str(entry.data.get(CONF_PASSPHRASE, DEFAULT_PASSPHRASE))
        self.link = MeshLink(hass, self.key, self._on_frame, self._on_raw)
        self.devices: dict[int, MeshDevice] = {
            int(hex_id, 16): MeshDevice(
                int(hex_id, 16),
                cfg["name"],
                DeviceKind(cfg[CONF_KIND]),
                model=cfg.get(CONF_MODEL),
                tag=_tag_from_cfg(cfg),
                tag_known=CONF_TAG in cfg,
                panel=cfg.get(CONF_PANEL, PANEL_TWO),
            )
            for hex_id, cfg in entry.options.get(CONF_DEVICES, {}).items()
        }
        self.min_level: int = entry.options.get(CONF_MIN_LEVEL, DEFAULT_MIN_LEVEL)
        self.available = False
        self.frames: deque[dict] = deque(maxlen=CAPTURE_BUFFER)
        self.seen: dict[int, SeenAddress] = {}
        self.decoded_count = 0
        self.raw_count = 0
        self._capturing = False
        self._captures = 0  # running capture calls; they may overlap
        # Random start: the SG gateway counts its transaction ids from 1 too,
        # and a device may ignore a command that reuses the id it just saw.
        self._tid = secrets.randbelow(256)
        self._inflight: dict[int, Delivery] = {}
        self.stats = {
            "sent": 0, "confirmed": 0, "confirmed_by_poll": 0, "retried": 0,
            "unconfirmed": 0, "overridden_by_hand": 0,
        }
        # latest-wins: a dragged slider only transmits its final value
        self._pending: dict[int, tuple[bool, int]] = {}
        self._resync: dict[int, asyncio.TimerHandle] = {}
        self._drain_task: asyncio.Task | None = None
        self._draining = False
        self._unsub_poll = None
        self._unsub_thermostats = None
        self._unsub_firmware = None
        self._observers: list = []
        self._trim_reads: dict[int, asyncio.Task] = {}

    # --- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self.link.start(self.entry)
        interval = self.entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        if interval > 0:
            self._unsub_poll = async_track_time_interval(
                self.hass, self._poll, timedelta(seconds=interval), name=f"{DOMAIN} status"
            )
        if any(d.kind == DeviceKind.THERMOSTAT for d in self.devices.values()):
            self._unsub_thermostats = async_track_time_interval(
                self.hass, self._poll_thermostats, timedelta(seconds=THERMOSTAT_POLL),
                name=f"{DOMAIN} thermostats",
            )
        self.entry.async_create_background_task(self.hass, self._poll(), f"{DOMAIN} first poll")
        self._unsub_firmware = async_call_later(self.hass, FIRMWARE_DELAY, self._start_firmware_read)

    async def async_stop(self) -> None:
        for unsub in (self._unsub_poll, self._unsub_thermostats, self._unsub_firmware):
            if unsub:
                unsub()
        for task in self._trim_reads.values():
            task.cancel()
        for handle in self._resync.values():
            handle.cancel()
        self._resync.clear()
        if self._drain_task:
            self._drain_task.cancel()
        await self.link.stop()

    def _notify(self, dev: MeshDevice) -> None:
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{dev.hex_id}")

    # --- inbound ----------------------------------------------------------

    @callback
    def _on_raw(self, char: str, data: bytes) -> None:
        """Every notification chunk, before decryption (capture mode only)."""
        if self._capturing:
            self.raw_count += 1
            self.frames.append({"t": time.time(), "raw": data.hex(), "char": char})

    @callback
    def _on_frame(self, frame: DecodedFrame) -> None:
        if not self.available:
            # Entities only redraw on their own signal; tell all of them.
            self.available = True
            for dev in self.devices.values():
                self._notify(dev)
        self.decoded_count += 1
        record = {
            "t": time.time(),
            "source": f"{frame.source:04x}",
            "ttl": frame.ttl,
            "payload": frame.payload.hex(),
        }
        if self._capturing:
            self.frames.append(record)
            _LOGGER.info("frame src=%s payload=%s", record["source"], record["payload"])
            self.hass.bus.async_fire(EVENT_FRAME, record)

        if (msg := parse_message(frame)) is None:
            return
        for observer in list(self._observers):
            try:
                observer(msg)
            except Exception:  # noqa: BLE001 - never let a watcher break RX
                _LOGGER.exception("observer failed")
        if msg.opcode in STATUS_OPS and msg.dest == ADDR_APP and msg.source != ADDR_APP:
            s = self.seen.get(msg.source)
            if s:
                s.body, s.last, s.count = msg.body.hex(), time.time(), s.count + 1
            else:
                self.seen[msg.source] = SeenAddress(msg.body.hex(), time.time())

        dev = self.devices.get(msg.source)
        if dev is None:
            return
        dev.last_seen = time.time()
        if msg.tag is not None and msg.dest in OWN_TAG_DESTS and (
            msg.tag != dev.tag or not dev.tag_known
        ):
            _LOGGER.debug("%04x uses tag %s", dev.mesh_id, msg.tag.hex())
            dev.tag, dev.tag_known = msg.tag, True
            self._persist_tag(dev)
        if dev.kind in (DeviceKind.DIMMER, DeviceKind.ONOFF):
            if msg.opcode == OP_TRIM and (levels := parse_trim(msg)):
                dev.trim = levels
                self._notify(dev)
            elif st := parse_light_status(msg):
                dev.on, dev.level = st.on, st.level
                self._track_delivery(dev, st.on, st.level, st.unsolicited)
                self._notify(dev)
        elif dev.kind == DeviceKind.SWITCH:
            self._on_switch_message(dev, msg)
        elif dev.kind == DeviceKind.THERMOSTAT:
            self._on_thermostat_message(dev, msg)

    def _on_switch_message(self, dev: MeshDevice, msg: Message) -> None:
        if (battery := parse_battery(msg)) is not None:
            if battery.level != dev.battery:
                dev.battery = battery.level
                self._notify(dev)
            return
        if (button := parse_switch_event(msg)) is None:
            return
        key = BUTTON_KEYS.get(dev.panel, BUTTON_KEYS[PANEL_TWO]).get(button)
        if key is not None and self._is_new_press(dev, key):
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{dev.hex_id}_button_{key}", "press")

    def _on_thermostat_message(self, dev: MeshDevice, msg: Message) -> None:
        if msg.dest != ADDR_APP:
            return
        if status := parse_status(msg):
            dev.on = status.on
            dev.extra.update(heating=status.heating, target=status.target)
            self._set_sw_version(dev.mesh_id, status.firmware)
            self._notify(dev)
        elif temps := parse_temperatures(msg):
            dev.extra.update(room=temps.room, floor=temps.floor, max_floor=temps.max_floor)
            self._notify(dev)

    def _track_delivery(self, dev: MeshDevice, on: bool, level: int, by_hand: bool) -> None:
        """Match a light status against the HA command in flight, if any."""
        d = self._inflight.get(dev.mesh_id)
        if by_hand:
            if d is None or not d.matches(on, level):
                # Used by hand (FF50/FF88). Whatever HA was sending is stale now.
                if d is not None and not d.overridden:
                    _LOGGER.info(
                        "%04x: changed by hand (%s, %s %%), dropping HA command",
                        dev.mesh_id, "on" if on else "off", level,
                    )
                    d.overridden = True
                    d.event.set()
                self._schedule_resync(dev.mesh_id)
                return
        if d is None:
            return
        if not by_hand:
            d.answered = True
        if d.matches(on, level):
            d.confirmed = True
            d.event.set()

    def _schedule_resync(self, mesh_id: int) -> None:
        """Read the device again once hands are off it (last report wins)."""
        if handle := self._resync.pop(mesh_id, None):
            handle.cancel()

        def _fire() -> None:
            self._resync.pop(mesh_id, None)
            self.entry.async_create_background_task(
                self.hass, self._poll_one(mesh_id), f"{DOMAIN} resync {mesh_id:04x}"
            )

        self._resync[mesh_id] = self.hass.loop.call_later(RESYNC_DELAY, _fire)

    def _persist_tag(self, dev: MeshDevice) -> None:
        """Keep the learned tag, so commands after a restart use it at once."""
        self.update_device_cfg(dev.mesh_id, {CONF_TAG: dev.tag.hex()})

    def device_cfg(self, mesh_id: int) -> dict:
        return self.entry.options.get(CONF_DEVICES, {}).get(f"{mesh_id:04x}", {})

    def update_device_cfg(self, mesh_id: int, changes: dict) -> None:
        """Merge ``changes`` into one device's stored options (no reload)."""
        devices = self.entry.options.get(CONF_DEVICES, {})
        key = f"{mesh_id:04x}"
        cfg = devices.get(key)
        if cfg is None or all(cfg.get(k) == v for k, v in changes.items()):
            return
        new = {k: dict(v) for k, v in devices.items()}
        new[key].update(changes)
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_DEVICES: new}
        )

    def _set_sw_version(self, mesh_id: int, version: str) -> None:
        """Record a firmware version read from the device, and show it in HA."""
        if self.device_cfg(mesh_id).get("sw_version") == version:
            return
        self.update_device_cfg(mesh_id, {"sw_version": version})
        dev_reg = dr.async_get(self.hass)
        if device := dev_reg.async_get_device(identifiers={(DOMAIN, f"{mesh_id:04x}")}):
            dev_reg.async_update_device(device.id, sw_version=version)

    # --- outbound ---------------------------------------------------------

    @staticmethod
    def _is_new_press(dev: MeshDevice, button: str) -> bool:
        """Turn FF15 messages into real presses.

        * The mesh delivers one press 2-3 times within a couple of seconds.
        * A switch joined by HA also repeats its last press by itself every
          ~36 s. A burst of the same button that starts one such period after
          the previous burst is treated as a repeat, not a press.
        """
        now = time.monotonic()
        copy_key, burst_key = f"copy_{button}", f"burst_{button}"
        last_copy = dev.extra.get(copy_key, -1e9)
        dev.extra[copy_key] = now
        if now - last_copy < PRESS_DEDUP:
            return False  # another copy of the same burst
        last_burst = dev.extra.get(burst_key, -1e9)
        dev.extra[burst_key] = now
        if REPEAT_MIN <= now - last_burst <= REPEAT_MAX:
            _LOGGER.debug("%04x: ignoring periodic repeat of button %s", dev.mesh_id, button)
            return False
        return True

    def _poll_tags(self, mesh_id: int) -> tuple[bytes, ...]:
        """A device ignores a poll with the wrong tag: try both until known."""
        dev = self.devices.get(mesh_id)
        if dev is None or dev.tag_known:
            return (self._tag(mesh_id),)
        return (SG_TAG, NO_TAG)

    def _tag(self, mesh_id: int) -> bytes:
        dev = self.devices.get(mesh_id)
        return dev.tag if dev else SG_TAG

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) & 0xFF
        return self._tid

    async def async_set(self, mesh_id: int, on: bool, level: int = 0) -> None:
        """on=True, level=0 -> on at the device's previous level."""
        dev = self.devices[mesh_id]
        dev.on = on  # optimistic; the device's FF50 report confirms
        if on and level:
            dev.level = level
        self._notify(dev)
        self._pending[mesh_id] = (on, level)
        # Flag, not task.done(): HA starts background tasks eagerly, so the
        # drain can already be running before the task handle is assigned.
        if not self._draining:
            self._draining = True
            self._drain_task = self.entry.async_create_background_task(
                self.hass, self._drain(), f"{DOMAIN} tx"
            )

    async def _drain(self) -> None:
        try:
            while self._pending:
                mesh_id, (on, level) = self._pending.popitem()
                try:
                    await self._deliver(mesh_id, on, level)
                    self.available = True
                except NoMeshNodeError as err:
                    self._set_unavailable(err)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("command to %04x failed: %s", mesh_id, err)
        finally:
            self._draining = False

    async def _deliver(self, mesh_id: int, on: bool, level: int) -> None:
        """Send until the device has the new state, gets used by hand, or we give up."""
        d = self._inflight[mesh_id] = Delivery(on, level)
        t0 = time.monotonic()
        try:
            for rnd in range(1, MAX_ROUNDS + 1):
                d.answered = False
                d.event.clear()
                packet_cmd = set_cmd(mesh_id, on, level, self._next_tid(), self._tag(mesh_id))
                for i in range(REPEATS):
                    await self.link.send(seal(self.key, packet_cmd))
                    self.stats["sent"] += 1
                    if d.event.is_set() or mesh_id in self._pending:
                        break
                    if i < REPEATS - 1:
                        await asyncio.sleep(REPEAT_GAP)
                if mesh_id in self._pending:
                    return  # superseded by a newer request; drain sends that
                if not d.event.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(d.event.wait(), CONFIRM_TIMEOUT)
                if not d.event.is_set() and mesh_id not in self._pending:
                    # No report: ask. Devices joined by HA never report a
                    # change that came from a command.
                    await self.link.send(seal(self.key, poll_cmd(mesh_id, self._tag(mesh_id))))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(d.event.wait(), POLL_WAIT)
                    if d.confirmed:
                        self.stats["confirmed_by_poll"] += 1
                if d.overridden:
                    self.stats["overridden_by_hand"] += 1
                    return
                if d.confirmed:
                    self.stats["confirmed"] += 1
                    _LOGGER.debug(
                        "%04x: confirmed after %.2f s (round %s)",
                        mesh_id, time.monotonic() - t0, rnd,
                    )
                    return
                if mesh_id in self._pending:
                    return
                if rnd < MAX_ROUNDS:
                    self.stats["retried"] += 1
                    _LOGGER.debug(
                        "%04x: %s after round %s, sending again", mesh_id,
                        "answer shows old state" if d.answered else "no answer", rnd,
                    )
            self.stats["unconfirmed"] += 1
            _LOGGER.info("%04x: no confirmation after %s rounds", mesh_id, MAX_ROUNDS)
        finally:
            if self._inflight.get(mesh_id) is d:
                del self._inflight[mesh_id]

    async def _poll_one(self, mesh_id: int) -> None:
        """FF04 to one device; its FF05 answer updates the state."""
        if mesh_id in self._inflight or mesh_id in self._pending:
            return  # a delivery is asking already
        try:
            await self.link.send(seal(self.key, poll_cmd(mesh_id, self._tag(mesh_id))))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("poll of %04x failed: %s", mesh_id, err)

    async def _poll(self, _now=None) -> None:
        try:
            targets = []
            for mesh_id, dev in self.devices.items():
                if dev.kind == DeviceKind.SWITCH:
                    continue  # asleep; it reports itself when pressed
                targets += [poll_cmd(mesh_id, tag) for tag in self._poll_tags(mesh_id)]
                if dev.kind == DeviceKind.THERMOSTAT:
                    targets.append(temperatures_get_cmd(mesh_id, self._tag(mesh_id)))
            targets = targets or [poll_cmd(ADDR_BROADCAST)]
            for i, cmd in enumerate(targets):
                last = i == len(targets) - 1
                await self.link.send(
                    seal(self.key, cmd),
                    listen=STATUS_LISTEN_TIME if last else POLL_SPACING,
                )
            if not self.available:
                # A node accepted our writes: the mesh is reachable.
                self.available = True
                for dev in self.devices.values():
                    self._notify(dev)
        except NoMeshNodeError as err:
            self._set_unavailable(err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("status poll failed: %s", err)

    async def _poll_thermostats(self, _now=None) -> None:
        """Set-point and state (FF04), room and floor temperature (FF3C 00)."""
        for mesh_id, dev in self.devices.items():
            if dev.kind != DeviceKind.THERMOSTAT:
                continue
            tag = self._tag(mesh_id)
            try:
                for cmd in (poll_cmd(mesh_id, tag), temperatures_get_cmd(mesh_id, tag)):
                    await self.link.send(seal(self.key, cmd), listen=POLL_SPACING)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("thermostat poll of %04x failed: %s", mesh_id, err)

    @callback
    def _start_firmware_read(self, _now=None) -> None:
        self._unsub_firmware = None
        self.entry.async_create_background_task(
            self.hass, self._read_all_firmware(), f"{DOMAIN} firmware"
        )

    async def _read_all_firmware(self) -> None:
        for mesh_id, dev in list(self.devices.items()):
            if (info := dev.model_info) is None or not info.firmware:
                continue
            try:
                await self.async_read_firmware(mesh_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("firmware read of %04x failed: %s", mesh_id, err)

    def _set_unavailable(self, err: Exception) -> None:
        if self.available:
            _LOGGER.warning("mesh unavailable: %s", err)
        self.available = False
        for dev in self.devices.values():
            self._notify(dev)

    @callback
    def _capture_raw(self, raw: bytes) -> None:
        """Log every reassembled packet our network key cannot open.

        Pairing, association and anything using another key shows up here.
        MASP (association) packets are recognised and decoded.
        """
        if len(raw) >= 14 and decrypt_packet(self.key, raw) is not None:
            return  # an ordinary mesh frame, already logged as "frame"
        from recsrmesh.crypto import derive_key, verify_packet_mac, xor_masp_packet

        payload, ok = verify_packet_mac(raw, derive_key("", "\x00MASP"))
        if ok:
            msg = xor_masp_packet(payload)
            _LOGGER.info("masp opcode=%02x msg=%s ttl=%02x", msg[0], msg.hex(), raw[-1])
            self.frames.append({"t": time.time(), "masp": msg.hex()})
        else:
            _LOGGER.info("undecoded raw=%s", raw.hex())
            self.frames.append({"t": time.time(), "undecoded": raw.hex()})

    # --- helpers for flows -----------------------------------------------

    def add_observer(self, observer) -> callable:
        """Call ``observer(Message)`` for every decoded message until removed."""
        self._observers.append(observer)
        return lambda: self._observers.remove(observer) if observer in self._observers else None

    async def async_blink(self, mesh_id: int, restore_on: bool | None = None,
                          restore_level: int = 0) -> None:
        """Flash a light so the user can see which one it is, then restore it."""
        tid = self._next_tid()
        seen = self.seen.get(mesh_id)
        if restore_on is None and seen:
            body = bytes.fromhex(seen.body)
            if len(body) >= 2 and body[0] in (0, 1):
                restore_on, restore_level = bool(body[0]), body[1]
        for on, level in ((not restore_on, 100), (bool(restore_on), restore_level)):
            tid = self._next_tid()
            for _ in range(2):
                await self.link.send(seal(self.key, set_cmd(mesh_id, on, level, tid, self._tag(mesh_id))))
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.0)

    async def async_factory_reset(self, mesh_id: int, wait_press: bool = False,
                                  timeout: float = 60.0) -> bool:
        """Make a device leave the mesh (CSRmesh disassociate), as the SG app
        does when a device is deleted. A battery switch only hears it while
        awake, so optionally wait for its next message first."""
        from .provision import disassociate_cmd

        cmd = [disassociate_cmd(mesh_id)]
        if wait_press:
            return await self.async_send_on_press(mesh_id, cmd, timeout=timeout)
        await self.async_send_raw(cmd, repeats=3)
        # A device that really left the mesh can no longer answer on it.
        # (Seen live: a dimmer set up by the SG gateway ignored this.)
        await asyncio.sleep(RESET_SETTLE)
        if await self.async_answers(mesh_id):
            _LOGGER.warning("%04x still answers after disassociate: not reset", mesh_id)
            raise StillInMeshError(f"{mesh_id:04x}")
        return True

    async def async_answers(self, mesh_id: int, wait: float = 3.0) -> bool:
        """Poll one device (both tags if needed); True if it answers."""
        heard = asyncio.Event()

        def watch(msg) -> None:
            if msg.source == mesh_id:
                heard.set()

        remove = self.add_observer(watch)
        try:
            for _ in range(2):
                for tag in (SG_TAG, NO_TAG):
                    await self.link.send(seal(self.key, poll_cmd(mesh_id, tag)))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(heard.wait(), wait / 2)
                if heard.is_set():
                    return True
            return False
        finally:
            remove()

    # --- commands with an answer -------------------------------------------

    async def async_request(self, message: bytes, match: Callable[[Message], bool], *,
                            tries: int = 3, wait: float | None = None) -> Message | None:
        """Send ``message`` (same bytes each time) until an answer matches.

        Returns the answer, or None if every try went unanswered. Silence is
        not a rejection, so the same message (same TID) is simply sent again;
        the device drops a duplicate it already acted on.
        """
        wait = REQUEST_WAIT if wait is None else wait
        loop = self.hass.loop
        answer: asyncio.Future[Message] = loop.create_future()

        def watch(msg: Message) -> None:
            if not answer.done() and match(msg):
                answer.set_result(msg)

        remove = self.add_observer(watch)
        try:
            for _ in range(tries):
                try:
                    await self.link.send(seal(self.key, message))
                except NoMeshNodeError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="mesh_unavailable"
                    ) from err
                with contextlib.suppress(TimeoutError):
                    return await asyncio.wait_for(asyncio.shield(answer), wait)
            return None
        finally:
            remove()
            if not answer.done():
                answer.cancel()

    # --- dimmer settings ----------------------------------------------------

    @callback
    def async_request_trim(self, mesh_id: int) -> None:
        """Read trim in the background; entities asking at once share one read."""
        if (task := self._trim_reads.get(mesh_id)) is not None and not task.done():
            return
        self._trim_reads[mesh_id] = self.entry.async_create_background_task(
            self.hass, self._read_trim(mesh_id), f"{DOMAIN} trim {mesh_id:04x}"
        )

    async def _read_trim(self, mesh_id: int) -> None:
        try:
            await self.async_read_trim(mesh_id)
        except Exception as err:  # noqa: BLE001 - background; the entity stays unknown
            _LOGGER.debug("trim read of %04x failed: %s", mesh_id, err)

    async def async_read_trim(self, mesh_id: int) -> TrimLevels | None:
        """FF2A -> FF2B; ``_on_frame`` stores the answer on the device."""
        await self.async_request(
            trim_get_cmd(mesh_id, self._tag(mesh_id)),
            lambda m: m.source == mesh_id and m.opcode == OP_TRIM,
        )
        return self.devices[mesh_id].trim

    async def async_set_trim(self, mesh_id: int, kind: TrimKind, percent: float) -> bool:
        """Set one trim level, then read back what the dimmer stored."""
        index = trim_index(kind, percent)
        tag = self._tag(mesh_id)
        if await self.async_request(
            trim_set_cmd(mesh_id, kind, index, tag), ack_from(mesh_id, OP_TRIM_SET, kind)
        ) is None:
            return False
        await self.async_read_trim(mesh_id)
        return True

    async def async_set_fading(self, mesh_id: int, tenths: int, fast: bool) -> bool:
        """Start-up fading and dimming speed (one command). Cannot be read back."""
        ok = await self.async_request(
            fading_cmd(mesh_id, tenths, fast, self._tag(mesh_id)),
            ack_from(mesh_id, OP_FADING, tenths),
        )
        if ok is not None:
            self.update_device_cfg(mesh_id, {CONF_FADING: tenths, CONF_SPEED: int(fast)})
            self._notify(self.devices[mesh_id])
        return ok is not None

    async def async_set_push_input(self, mesh_id: int, sensor: bool) -> bool:
        """External push-button input: 0 push button, 1 sensor. Cannot be read back."""
        ok = await self.async_request(
            push_switch_cmd(mesh_id, sensor, self._tag(mesh_id)),
            ack_from(mesh_id, OP_PUSH_SWITCH, int(sensor)),
        )
        if ok is not None:
            self.update_device_cfg(mesh_id, {CONF_PUSH: int(sensor)})
            self._notify(self.devices[mesh_id])
        return ok is not None

    async def async_read_firmware(self, mesh_id: int) -> str | None:
        msg = await self.async_request(
            firmware_get_cmd(mesh_id, self._tag(mesh_id)),
            lambda m: m.source == mesh_id and m.opcode == OP_FIRMWARE,
            tries=2,
        )
        if msg is None or (version := parse_firmware(msg)) is None:
            return None
        self._set_sw_version(mesh_id, version)
        return version

    # --- thermostats --------------------------------------------------------

    async def async_set_thermostat_target(self, mesh_id: int, celsius: float) -> bool:
        cmd = setpoint_cmd(mesh_id, celsius, self._next_tid(), self._tag(mesh_id))
        if await self.async_request(cmd, ack_from(mesh_id, OP_SETPOINT)) is None:
            return False
        dev = self.devices[mesh_id]
        dev.extra["target"] = round(cmd[6] + cmd[7] / 10, 1)
        self._notify(dev)
        self.entry.async_create_background_task(
            self.hass, self._poll_one(mesh_id), f"{DOMAIN} check {mesh_id:04x}"
        )
        return True

    async def async_set_thermostat_power(self, mesh_id: int, on: bool) -> bool:
        """FF42 has no acknowledgement: ask (FF04) until the status shows it."""
        tag = self._tag(mesh_id)
        cmd = power_cmd(mesh_id, on, self._next_tid(), tag)

        def shows_it(msg: Message) -> bool:
            if msg.source != mesh_id or msg.dest != ADDR_APP:
                return False
            status = parse_status(msg)
            return status is not None and status.on == on

        for _ in range(3):
            try:
                await self.link.send(seal(self.key, cmd))
            except NoMeshNodeError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="mesh_unavailable"
                ) from err
            await asyncio.sleep(REPEAT_GAP)
            if await self.async_request(poll_cmd(mesh_id, tag), shows_it, tries=1, wait=POLL_WAIT):
                return True
        return False

    # --- Smart Switch configuration ---------------------------------------

    def pair_steps(self, switch_id: int, buttons: tuple[int, ...],
                   target: int) -> list[list[SwitchStep]]:
        """Clear, then set, each rocker: the SG app's sequence (docs 6.1)."""
        tag = self._tag(switch_id)
        return [
            [
                SwitchStep(
                    lambda tid, b=b: switch_clear_cmd(switch_id, b, tid, tag),
                    ack_from(switch_id, OP_SWITCH_CLEAR, b),
                ),
                SwitchStep(
                    lambda tid, b=b: switch_button_cmd(switch_id, b, target, tid, tag=tag),
                    ack_from(switch_id, OP_SWITCH_BUTTON, b),
                ),
            ]
            for b in buttons
        ]

    def unpair_steps(self, switch_id: int, buttons: tuple[int, ...]) -> list[list[SwitchStep]]:
        tag = self._tag(switch_id)
        return [
            [
                SwitchStep(
                    lambda tid, b=b: switch_clear_cmd(switch_id, b, tid, tag),
                    ack_from(switch_id, OP_SWITCH_CLEAR, b),
                )
            ]
            for b in buttons
        ]

    async def async_configure_switch(self, switch_id: int, units: list[list[SwitchStep]],
                                     timeout: float = SWITCH_TIMEOUT) -> bool:
        """Configure a battery switch while it is awake after a press.

        Each press sends: wake (FF12 01), every unfinished unit, sleep (FF12
        00), back to back as the SG app does (each GATT write through a proxy
        takes ~0.4 s and the switch listens for ~2 s). A unit is done when
        all its commands were acknowledged; what is left goes out on the next
        press. True when every unit is done, False if no press came in time.
        """
        tag = self._tag(switch_id)
        pending = list(units)
        woke = asyncio.Event()
        acks: list[Message] = []

        def watch(msg: Message) -> None:
            if msg.source != switch_id:
                return
            if msg.opcode == OP_ACK:
                acks.append(msg)
            else:
                woke.set()

        loop = self.hass.loop
        deadline = loop.time() + timeout
        remove = self.add_observer(watch)
        try:
            while pending:
                woke.clear()
                try:
                    await asyncio.wait_for(woke.wait(), max(0.0, deadline - loop.time()))
                except TimeoutError:
                    _LOGGER.info("%04x: no press, %d step(s) not sent", switch_id, len(pending))
                    return False
                acks.clear()
                t0 = time.monotonic()
                messages = [switch_awake_cmd(switch_id, True, self._next_tid(), tag)]
                messages += [step.build(self._next_tid()) for unit in pending for step in unit]
                messages.append(switch_awake_cmd(switch_id, False, self._next_tid(), tag))
                await self.async_send_raw(messages, gap=0, repeats=1, t0=t0)
                await asyncio.sleep(SWITCH_ACK_SETTLE)
                pending = [
                    unit for unit in pending
                    if not all(any(step.match(a) for a in acks) for step in unit)
                ]
                if pending:
                    _LOGGER.info(
                        "%04x: %d unit(s) not acknowledged; waiting for the next press",
                        switch_id, len(pending),
                    )
            return True
        finally:
            remove()

    # --- developer tools --------------------------------------------------

    async def async_send_raw(self, messages: list[bytes], gap: float = 0.3,
                             repeats: int = 2, t0: float | None = None) -> None:
        """Seal and send MCP messages as given (dest + opcode + body).

        No pause after the last copy of the last message: a battery switch
        is only awake for about two seconds after a press, and the SG app
        fits wake, clear, set and sleep into 1.7 s.
        """
        t0 = time.monotonic() if t0 is None else t0
        for i, msg in enumerate(messages):
            for n in range(repeats):
                await self.link.send(seal(self.key, msg))
                if n < repeats - 1:
                    await asyncio.sleep(0.12)
            _LOGGER.info("sent raw %s (+%.2f s)", msg.hex(), time.monotonic() - t0)
            if gap and i < len(messages) - 1:
                await asyncio.sleep(gap)

    async def async_send_on_press(self, source: int, messages: list[bytes],
                                  timeout: float = 60.0, gap: float = 0.3,
                                  repeats: int = 2) -> bool:
        """Wait for any message from ``source`` (a battery switch is awake for a
        few seconds after a press), then send ``messages`` right away.

        For a configuration sequence use ``repeats=1, gap=0``: each GATT write
        through a proxy takes ~0.4 s, so two copies per message miss the
        switch's wake window."""
        pressed = asyncio.Event()

        def watch(msg) -> None:
            if msg.source == source:
                pressed.set()

        remove = self.add_observer(watch)
        try:
            try:
                await asyncio.wait_for(pressed.wait(), timeout)
            except TimeoutError:
                _LOGGER.info("send_on_press: no message from %04x", source)
                return False
        finally:
            remove()
        t0 = time.monotonic()
        _LOGGER.info("send_on_press: %04x is awake, sending %d message(s)", source, len(messages))
        await self.async_send_raw(messages, gap=gap, repeats=repeats, t0=t0)
        return True

    # --- capture ----------------------------------------------------------

    async def async_capture(self, seconds: float) -> None:
        """Record every notification on *your* network for ``seconds``.

        Raw chunks and decrypted frames both go into the diagnostics buffer;
        decrypted frames are also logged and fired as ``sg_csrmesh_frame``.
        """
        # Captures may overlap (a new one started before the last ended); only
        # the last one to finish switches capturing off again.
        self._captures += 1
        self._capturing = True
        self.raw_count = 0
        self.decoded_count = 0
        self.link.set_raw_sink(self._capture_raw)
        try:
            await self.link.send(seal(self.key, poll_cmd(ADDR_BROADCAST)))
            await self.link.listen(seconds)
        except Exception as err:  # noqa: BLE001 - surface any link failure to the UI
            raise HomeAssistantError(f"capture failed: {err}") from err
        finally:
            self._captures -= 1
            if self._captures == 0:
                self._capturing = False
                self.link.set_raw_sink(None)
            _LOGGER.info(
                "capture done: %s raw notifications, %s decrypted frames",
                self.raw_count, self.decoded_count,
            )
