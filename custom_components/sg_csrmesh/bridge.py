"""One persistent GATT link into the CSRmesh, via HA's Bluetooth stack.

Any mesh node (@NDxxxx, @MDxxxx, ...) relays into the whole mesh. Setting up a
link costs 3-15 s through a proxy, so the link is kept open permanently and a
background keeper reconnects as soon as it drops. Commands then only pay for
the GATT write. All access is serialised: one radio, one link, one writer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .csrmesh.crypto import DecodedFrame, split_for_gatt
from .csrmesh.protocol import (
    CHAR_8003,
    CHAR_8004,
    SERVICE_UUID,
    FrameAssembler,
    is_node_name,
)

_LOGGER = logging.getLogger(__name__)

RSSI_HYSTERESIS = 8  # dB, don't flap between bridge nodes
MAX_NODES_PER_ATTEMPT = 3
QUARANTINE_S = 300  # skip a node that failed to connect for this long
RECONNECT_BACKOFF = (1, 3, 10, 30, 60)  # s between keeper attempts


def is_mesh_node(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    return is_node_name(info.name) or SERVICE_UUID in (
        u.lower() for u in info.service_uuids
    )


class NoMeshNodeError(Exception):
    """No connectable mesh node is visible to any proxy/adapter."""


class MeshLink:
    def __init__(
        self,
        hass: HomeAssistant,
        key: bytes,
        on_frame: Callable[[DecodedFrame], None],
        on_raw: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self._hass = hass
        self._assembler = FrameAssembler(key)
        self._on_frame = on_frame
        self._on_raw = on_raw
        self._lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._address: str | None = None
        self._quarantine: dict[str, float] = {}
        # A node chosen by hand (association of a device the mesh cannot
        # reach): used instead of the best node until released.
        self._pinned: str | None = None
        self._pin_timer: asyncio.TimerHandle | None = None
        self._raw_sink: Callable[[bytes], None] | None = None
        self._raw_head: bytes | None = None
        self._raw_timer: asyncio.TimerHandle | None = None
        self._dropped = asyncio.Event()
        self._keeper: asyncio.Task | None = None
        self._stopping = False
        self.connects = 0
        self.drops = 0
        self.last_connect_s: float | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def bridge_address(self) -> str | None:
        return self._address

    @property
    def quarantined(self) -> list[str]:
        now = time.monotonic()
        return [a for a, t in self._quarantine.items() if t > now]

    # --- node selection ---------------------------------------------------

    @callback
    def visible_nodes(
        self, connectable: bool = True, fresh_only: bool = True
    ) -> list[bluetooth.BluetoothServiceInfoBleak]:
        """Mesh nodes seen by any adapter/proxy, strongest first.

        A node is recognised by its @XXxxxx name OR the CSRmesh service UUID.
        With ``fresh_only`` a node must still be advertising now: the
        discovery history keeps nodes long after they went quiet, and
        connecting to one of those burns minutes on retries.
        """
        return sorted(
            (
                info
                for info in bluetooth.async_discovered_service_info(self._hass, connectable=connectable)
                if is_mesh_node(info)
                and (
                    not fresh_only
                    or bluetooth.async_address_present(self._hass, info.address, connectable)
                )
            ),
            key=lambda i: i.rssi,
            reverse=True,
        )

    def _candidates(self) -> list[BLEDevice]:
        """Nodes to try, best first; current bridge kept unless clearly beaten."""
        if self._pinned:
            device = bluetooth.async_ble_device_from_address(
                self._hass, self._pinned, connectable=True
            )
            if device is None:
                raise NoMeshNodeError(
                    f"{self._pinned} is not advertising within reach of a connectable "
                    "Bluetooth adapter/proxy"
                )
            return [device]
        now = time.monotonic()
        self._quarantine = {a: t for a, t in self._quarantine.items() if t > now}
        nodes = [n for n in self.visible_nodes() if n.address not in self._quarantine]
        if not nodes:
            raise NoMeshNodeError(
                "no CSRmesh node currently advertising within reach of a connectable "
                "Bluetooth adapter/proxy"
            )
        if self._address:
            current = next((n for n in nodes if n.address == self._address), None)
            if current and nodes[0].rssi - current.rssi < RSSI_HYSTERESIS:
                nodes.remove(current)
                nodes.insert(0, current)
        return [n.device for n in nodes[:MAX_NODES_PER_ATTEMPT]]

    # --- link lifecycle ---------------------------------------------------

    async def _ensure(self) -> BleakClientWithServiceCache:
        if self.connected:
            return self._client  # type: ignore[return-value]
        last_err: Exception | None = None
        for device in self._candidates():
            try:
                client = await self._connect(device)
            except Exception as err:  # noqa: BLE001 - bleak raises many types
                _LOGGER.info(
                    "mesh node %s (%s) unreachable, trying next: %s",
                    device.name, device.address, err,
                )
                self._quarantine[device.address] = time.monotonic() + QUARANTINE_S
                last_err = err
                continue
            return client
        raise last_err or NoMeshNodeError("no mesh node reachable")

    async def _connect(self, device: BLEDevice) -> BleakClientWithServiceCache:
        t0 = time.monotonic()
        await close_stale_connections_by_address(device.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or device.address,
            disconnected_callback=self._disconnected,
            max_attempts=2,
        )
        try:
            self._assembler.reset()
            for char in (CHAR_8003, CHAR_8004):
                await client.start_notify(char, self._notify)
        except Exception:
            await client.disconnect()
            raise
        self._client, self._address = client, device.address
        self._dropped.clear()
        self.connects += 1
        self.last_connect_s = round(time.monotonic() - t0, 1)
        _LOGGER.info(
            "connected to mesh node %s (%s) in %.1f s",
            device.name, device.address, self.last_connect_s,
        )
        return client

    def _notify(self, char, data: bytearray) -> None:
        uuid = str(getattr(char, "uuid", char))
        tag = uuid.split("-")[3] if uuid.count("-") == 4 else uuid
        chunk = bytes(data)
        if self._on_raw:
            self._hass.loop.call_soon_threadsafe(self._on_raw, tag, chunk)
        if self._raw_sink is not None:
            self._hass.loop.call_soon_threadsafe(self._feed_raw_sink, tag, chunk)
        for frame in self._assembler.feed(tag, chunk):
            self._hass.loop.call_soon_threadsafe(self._on_frame, frame)

    # --- raw (MASP) mode ----------------------------------------------------

    def set_raw_sink(self, sink: Callable[[bytes], None] | None) -> None:
        """Also hand every reassembled raw packet to ``sink`` (association).

        MTL reassembly as the reference implementation does it: an 8003
        chunk waits up to 200 ms for its 8004 tail; an 8004 chunk on its own
        is a complete short packet.
        """
        self._raw_sink = sink
        self._raw_head = None
        if self._raw_timer:
            self._raw_timer.cancel()
            self._raw_timer = None

    def _feed_raw_sink(self, tag: str, chunk: bytes) -> None:
        sink = self._raw_sink
        if sink is None:
            return
        if tag.endswith("8003"):
            if self._raw_head is not None:
                sink(self._raw_head)
                self._raw_head = None
            if len(chunk) < 20:
                # An MTL head is always 20 bytes; a shorter 8003 chunk is a
                # whole packet. Waiting for a tail would glue it to the tail
                # of some other frame on a busy mesh.
                sink(chunk)
                return
            self._raw_head = chunk
            if self._raw_timer:
                self._raw_timer.cancel()
            self._raw_timer = self._hass.loop.call_later(0.2, self._flush_raw_head)
            return
        if self._raw_timer:
            self._raw_timer.cancel()
            self._raw_timer = None
        head, self._raw_head = self._raw_head, None
        sink(head + chunk if head is not None else chunk)

    def _flush_raw_head(self) -> None:
        self._raw_timer = None
        head, self._raw_head = self._raw_head, None
        if head is not None and self._raw_sink is not None:
            self._raw_sink(head)

    async def write_raw(self, char_uuid: str, data: bytes) -> None:
        """Write one MTL chunk as-is."""
        async with self._lock:
            client = await self._ensure()
            await client.write_gatt_char(char_uuid, data, response=False)

    async def write_packet_raw(self, packet: bytes) -> None:
        """Write a whole packet (MTL-split) atomically, without response.

        Used for association, where the reference implementations write
        without response and every round trip counts.
        """
        async with self._lock:
            client = await self._ensure()
            for char, chunk in split_for_gatt(packet):
                await client.write_gatt_char(
                    CHAR_8003 if char == "8003" else CHAR_8004, chunk, response=False
                )

    @property
    def pinned(self) -> str | None:
        return self._pinned

    async def pin(self, address: str | None, hold: float = 600.0) -> None:
        """Connect to ``address`` and stay there (``None``: back to the best node).

        Used to associate a factory reset device directly, as the SG app does
        from a phone: the device answers on its own connection. Released by
        itself after ``hold`` seconds, so a forgotten pin cannot strand the link.
        """
        if self._pin_timer:
            self._pin_timer.cancel()
            self._pin_timer = None
        async with self._lock:
            self._pinned = address
            if self.connected and self._address != address:
                await self.disconnect()
            if address is not None:
                self._quarantine.pop(address, None)
                await self._ensure()
                self._pin_timer = self._hass.loop.call_later(
                    hold,
                    lambda: self._hass.async_create_task(self.pin(None), "sg_csrmesh unpin"),
                )
        if address is None:
            self._dropped.set()  # the keeper reconnects to the best node

    def _disconnected(self, _client) -> None:
        _LOGGER.debug("mesh link dropped")
        self._client = None
        self.drops += 1
        self._hass.loop.call_soon_threadsafe(self._dropped.set)

    # --- keeper -----------------------------------------------------------

    def start(self, entry) -> None:
        """Keep a link open for as long as the config entry is loaded."""
        self._stopping = False
        self._keeper = entry.async_create_background_task(
            self._hass, self._keep(), "sg_csrmesh link keeper"
        )

    async def _keep(self) -> None:
        failures = 0
        while not self._stopping:
            if self.connected:
                failures = 0
                await self._dropped.wait()
                continue
            try:
                async with self._lock:
                    await self._ensure()
                failures = 0
            except Exception as err:  # noqa: BLE001 - keep trying, quietly
                delay = RECONNECT_BACKOFF[min(failures, len(RECONNECT_BACKOFF) - 1)]
                failures += 1
                _LOGGER.debug("keeper: no link (%s), retry in %s s", err, delay)
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stopping = True
        if self._pin_timer:
            self._pin_timer.cancel()
            self._pin_timer = None
        self._dropped.set()
        if self._keeper:
            self._keeper.cancel()
        await self.disconnect()

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("disconnect failed", exc_info=True)

    # --- I/O --------------------------------------------------------------

    async def send(self, packet: bytes, *, listen: float = 0.0) -> None:
        """Write one sealed frame, then optionally wait while replies arrive."""
        async with self._lock:
            for attempt in (1, 2):
                try:
                    client = await self._ensure()
                    for char, chunk in split_for_gatt(packet):
                        await client.write_gatt_char(
                            CHAR_8003 if char == "8003" else CHAR_8004, chunk, response=True
                        )
                    break
                except NoMeshNodeError:
                    raise
                except Exception as err:  # noqa: BLE001 - bleak raises many types
                    _LOGGER.debug("write attempt %s failed: %s", attempt, err)
                    await self.disconnect()
                    if attempt == 2:
                        raise
        # Replies are delivered by notifications; no need to hold the lock.
        if listen:
            await asyncio.sleep(listen)

    async def listen(self, seconds: float) -> None:
        """Collect notifications for a while (used by capture)."""
        loop = self._hass.loop
        deadline = loop.time() + seconds
        while (left := deadline - loop.time()) > 0:
            if not self.connected:
                async with self._lock:
                    await self._ensure()
            await asyncio.sleep(min(1.0, left))
