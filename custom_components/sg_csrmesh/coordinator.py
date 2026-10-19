"""Coordinator: owns the mesh link, device list, state and the status poll."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .bridge import MeshLink, NoMeshNodeError
from .const import (
    CAPTURE_BUFFER,
    CONF_DEVICES,
    CONF_KIND,
    CONF_MIN_LEVEL,
    CONF_NETWORK_KEY,
    CONF_PASSPHRASE,
    CONF_POLL_INTERVAL,
    CONF_TAG,
    DEFAULT_MIN_LEVEL,
    DEFAULT_PASSPHRASE,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_FRAME,
    SIGNAL_UPDATE,
    STATUS_LISTEN_TIME,
)
from .csrmesh.crypto import (
    DecodedFrame,
    decrypt_packet,
    network_key_from_passphrase,
    parse_key_hex,
)
from .csrmesh.protocol import (
    ADDR_APP,
    ADDR_BROADCAST,
    NO_TAG,
    OWN_TAG_DESTS,
    SG_TAG,
    STATUS_OPS,
    DeviceKind,
    parse_light_status,
    parse_message,
    parse_switch_event,
    parse_thermostat,
    poll_cmd,
    seal,
    set_cmd,
)

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
    model: str | None = None
    tag: bytes = SG_TAG  # learned from the device's own messages, persisted
    tag_known: bool = False  # stored or heard; until then polls try both

    @property
    def hex_id(self) -> str:
        return f"{self.mesh_id:04x}"


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
                model=cfg.get("model"),
                tag=_tag_from_cfg(cfg),
                tag_known=CONF_TAG in cfg,
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
        self._observers: list = []

    # --- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self.link.start(self.entry)
        interval = self.entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        if interval > 0:
            self._unsub_poll = async_track_time_interval(
                self.hass, self._poll, timedelta(seconds=interval), name=f"{DOMAIN} status"
            )
        self.entry.async_create_background_task(self.hass, self._poll(), f"{DOMAIN} first poll")

    async def async_stop(self) -> None:
        if self._unsub_poll:
            self._unsub_poll()
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
        if dev.kind in (DeviceKind.DIMMER, DeviceKind.ONOFF) and (st := parse_light_status(msg)):
            dev.on, dev.level = st.on, st.level
            self._track_delivery(dev, st.on, st.level, st.unsolicited)
            self._notify(dev)
        elif dev.kind == DeviceKind.SWITCH and (button := parse_switch_event(msg)) is not None:
            if self._is_new_press(dev, button):
                async_dispatcher_send(
                    self.hass, f"{SIGNAL_UPDATE}_{dev.hex_id}_button_{button}", "press"
                )
        elif dev.kind == DeviceKind.THERMOSTAT and parse_thermostat(msg) is not None:
            # TODO: -> {"current", "target", "mode"} into dev.extra
            pass

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
        devices = self.entry.options.get(CONF_DEVICES, {})
        cfg = devices.get(dev.hex_id)
        if cfg is None or cfg.get(CONF_TAG) == dev.tag.hex():
            return
        new = {k: dict(v) for k, v in devices.items()}
        new[dev.hex_id][CONF_TAG] = dev.tag.hex()
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_DEVICES: new}
        )

    # --- outbound ---------------------------------------------------------

    @staticmethod
    def _is_new_press(dev: MeshDevice, button: int) -> bool:
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
            targets = [
                poll_cmd(mesh_id, tag)
                for mesh_id in self.devices
                for tag in self._poll_tags(mesh_id)
            ] or [poll_cmd(ADDR_BROADCAST)]
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
