"""The mesh service: listener boards in, insteon-mqtt out.

Wires the pieces together::

    boards ──MQTT──> MqttReceiver ──> Fusion ──> Injector ──MQTT──> insteon-mqtt
                                        ^                               │
    insteon/raw/rx <────────────────────┘  (what the PLM already heard)  │
                                                                        v
                             insteon-rf-mesh.jsonl + the miss table  ───┘

:class:`MissTable` is the point of the whole exercise before any injection is
switched on: it answers "how often does the PLM actually miss something, and
for which devices". If the answer turns out to be "almost never", that is a
real result and the right move is to stop there rather than build the rest.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .context import CommandTracker
from .fusion import FusedEvent, Fusion, sightings_from_capture
from .inject import Injector, PlmMemory, sender_of
from .monitor import JsonlWriter
from .packet import Address
from .radio.mqtt import Capture, MqttReceiver

log = logging.getLogger(__name__)

#: Where insteon-mqtt's patched build mirrors everything the modem reads.
PLM_RX_TOPIC = "insteon/raw/rx"
#: Where it accepts messages to feed into the protocol stack.
PLM_INJECT_TOPIC = "insteon/raw/inject"


# --------------------------------------------------------------------------- miss table


@dataclass
class DeviceMisses:
    """Per-device tally of the two paths."""

    heard_by_rf: int = 0
    plm_also_heard: int = 0
    plm_missed: int = 0
    injected: int = 0
    best_rssi_dbm: float | None = None
    receivers: set[str] = field(default_factory=set)

    @property
    def miss_rate(self) -> float:
        return self.plm_missed / self.heard_by_rf if self.heard_by_rf else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "heard_by_rf": self.heard_by_rf,
            "plm_also_heard": self.plm_also_heard,
            "plm_missed": self.plm_missed,
            "injected": self.injected,
            "miss_rate": round(self.miss_rate, 4),
            "best_rssi_dbm": self.best_rssi_dbm,
            "receivers": sorted(self.receivers),
        }


class MissTable:
    """How often each device was heard on RF but not by the modem."""

    def __init__(self, plm_addr: Address | str | None = None) -> None:
        self.devices: dict[str, DeviceMisses] = {}
        self.started = time.time()
        # The modem's own transmissions are heard on RF but never come back
        # as inbound messages (they are reported as 0x62 echoes), so counting
        # them makes the modem look like the device it misses most. They are
        # also the one thing that must never be injected.
        self.plm_addr = Address(plm_addr) if plm_addr is not None else None
        self.plm_own_transmissions = 0
        #: Fused events whose bytes never verified. Reported, not attributed.
        self.undecodable = 0

    def note(self, event: FusedEvent, injected: bool = False) -> None:
        # Only count messages that actually decoded. A capture can yield a
        # four-byte fragment (enough to look like a packet start, not enough
        # to name a sender or carry a CRC) and counting those would inflate
        # every device's "heard on RF" total with noise.
        if event.packet.crc_ok is not True:
            self.undecodable += 1
            return
        who = sender_of(event.packet)
        if who is None:
            return
        if self.plm_addr is not None and who == self.plm_addr:
            self.plm_own_transmissions += 1
            return
        d = self.devices.setdefault(str(who), DeviceMisses())
        d.heard_by_rf += 1
        if event.plm_saw_it is True:
            d.plm_also_heard += 1
        elif event.plm_saw_it is False:
            d.plm_missed += 1
        if injected:
            d.injected += 1
        d.receivers.update(event.receivers)
        for view in event.receivers.values():
            if view.rssi_dbm is not None and (
                d.best_rssi_dbm is None or view.rssi_dbm > d.best_rssi_dbm
            ):
                d.best_rssi_dbm = view.rssi_dbm

    def report(self, *, min_seen: int = 1) -> str:
        """A table, worst miss rate first."""
        rows = [
            (addr, d)
            for addr, d in self.devices.items()
            if d.heard_by_rf >= min_seen
        ]
        rows.sort(key=lambda kv: (-kv[1].miss_rate, -kv[1].heard_by_rf))
        hours = (time.time() - self.started) / 3600.0
        out = [
            f"# miss table over {hours:.1f}h, {len(rows)} devices heard on RF"
            + (f", {self.undecodable} undecodable" if self.undecodable else "")
            + (f", {self.plm_own_transmissions} from the modem itself"
               if self.plm_own_transmissions else ""),
            f"{'device':12} {'rf':>5} {'plm':>5} {'missed':>7} {'rate':>6} "
            f"{'rssi':>6}  receivers",
        ]
        for addr, d in rows:
            rssi = f"{d.best_rssi_dbm:.0f}" if d.best_rssi_dbm is not None else "-"
            out.append(
                f"{addr:12} {d.heard_by_rf:5d} {d.plm_also_heard:5d} "
                f"{d.plm_missed:7d} {d.miss_rate:6.1%} {rssi:>6}  "
                f"{','.join(sorted(d.receivers))}"
            )
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "hours": round((time.time() - self.started) / 3600.0, 3),
            "undecodable": self.undecodable,
            "plm_own_transmissions": self.plm_own_transmissions,
            "devices": {a: d.to_dict() for a, d in self.devices.items()},
        }


# --------------------------------------------------------------------------- PLM link


class PlmLink:
    """Subscribe to what the modem heard; publish what it missed.

    Its own MQTT client, separate from the capture receiver's. Two clients
    rather than one because the two directions have genuinely different
    lifetimes and failure meanings: losing captures degrades coverage, while
    losing this link must stop injection entirely (we would no longer know
    what the PLM already heard, and suppression would silently stop working).
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        username: str | None = None,
        password: str | None = None,
        rx_topic: str = PLM_RX_TOPIC,
        inject_topic: str = PLM_INJECT_TOPIC,
        client_id: str | None = None,
        on_frame: Callable[..., Any] | None = None,
    ):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:  # pragma: no cover - optional dependency
            raise SystemExit(
                "paho-mqtt is not installed: pip install 'insteonrf[mqtt]'"
            ) from err

        self.rx_topic = rx_topic
        self.inject_topic = inject_topic
        self.on_frame: Callable[..., Any] | None = on_frame
        self.frames = 0
        self.injected = 0
        self.last_frame_at: float | None = None

        # A fixed client id means two instances evict each other in a loop,
        # each reconnect re-subscribing -- easy to mistake for a broker fault.
        if client_id is None:
            client_id = "insteon-rf-plm-" + uuid.uuid4().hex[:8]
        api = getattr(mqtt, "CallbackAPIVersion", None)
        self.client = (
            mqtt.Client(api.VERSION2, client_id=client_id)
            if api is not None
            else mqtt.Client(client_id=client_id)
        )
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect_async(host, port)
        self.client.loop_start()
        log.info("watching %s on mqtt://%s:%d", rx_topic, host, port)

    def _on_connect(self, client: Any, *_: Any, **__: Any) -> None:
        client.subscribe(self.rx_topic, qos=0)

    def _on_message(self, _c: Any, _u: Any, message: Any) -> None:
        try:
            rec = json.loads(message.payload.decode("utf-8"))
            raw = bytes.fromhex(rec["raw"])
        except Exception as err:
            log.warning("bad frame on %s: %r", message.topic, err)
            return
        self.frames += 1
        self.last_frame_at = time.time()
        if self.on_frame is not None:
            self.on_frame(raw)

    def publish_inject(self, frame: bytes, event: FusedEvent) -> None:
        payload: dict[str, Any] = {
            "raw": frame.hex().upper(),
            "src": event.closest or "insteon-rf",
            "receivers": sorted(event.receivers),
        }
        for name, view in event.receivers.items():
            if name == event.closest and view.rssi_dbm is not None:
                payload["rssi"] = view.rssi_dbm
        self.client.publish(
            self.inject_topic, json.dumps(payload, separators=(",", ":")), qos=1
        )
        self.injected += 1

    def healthy(self, *, max_silence_s: float = 900.0) -> bool:
        """Whether the modem's mirror is still arriving.

        Silence here is ambiguous — a quiet house looks the same as a broken
        link — so this is only a hint, and the caller decides what to do.
        """
        if self.last_frame_at is None:
            return False
        return (time.time() - self.last_frame_at) < max_silence_s

    def close(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception as err:  # pragma: no cover - best effort
            log.debug("mqtt disconnect: %r", err)


# --------------------------------------------------------------------------- service


class MeshService:
    """Pull captures, fuse them, decide, record."""

    def __init__(
        self,
        receiver: MqttReceiver,
        *,
        injector: Injector | None = None,
        plm: PlmLink | None = None,
        fusion: Fusion | None = None,
        writer: JsonlWriter | None = None,
        tracker: CommandTracker | None = None,
        require_plm_link: bool = True,
        on_event: Callable[[FusedEvent], None] | None = None,
        memory: PlmMemory | None = None,
        plm_addr: Address | str | None = None,
    ):
        self.receiver = receiver
        self.injector = injector
        self.plm = plm
        self.fusion = fusion if fusion is not None else Fusion()
        self.writer = writer
        self.tracker = tracker if tracker is not None else CommandTracker()
        self.require_plm_link = require_plm_link
        self.on_event = on_event
        self.misses = MissTable(
            plm_addr if plm_addr is not None
            else (injector.plm_addr if injector is not None else None)
        )
        self.captures = 0
        self.packets = 0
        self.events = 0
        # One shared record of what the modem heard. The comparison has to
        # work with injection switched off, because measuring the miss rate is
        # what decides whether injection is worth enabling at all -- so this
        # lives on the service, and the injector borrows it rather than owning
        # it.
        if memory is not None:
            self.memory = memory
        elif injector is not None:
            self.memory = injector.memory
        else:
            self.memory = PlmMemory()
        if injector is not None:
            injector.memory = self.memory
        if plm is not None:
            plm.on_frame = self.memory.note

    def handle_capture(self, cap: Capture) -> int:
        """Decode one capture into sightings. Returns how many packets it held."""
        self.captures += 1
        found = sightings_from_capture(
            cap.bits, cap.receiver, rssi_dbm=cap.rssi_dbm, timestamp=cap.timestamp,
            soft=cap.soft,
        )
        for s in found:
            self.tracker.observe(s.packet)
            self.fusion.add(s)
        self.packets += len(found)
        return len(found)

    def drain(self, now: float | None = None) -> list[FusedEvent]:
        """Close finished buckets, decide on each, record, return them."""
        out = []
        for event in self.fusion.pop_ready(now):
            self.events += 1
            # Always answer "did the modem hear this too", injector or not.
            event.plm_saw_it = self.memory.heard(event.key, now)
            injected = False
            if self.injector is not None:
                if self.require_plm_link and self.plm is not None and not self.plm.healthy():
                    # Without the modem's mirror we cannot tell what it
                    # already heard, and injecting blind would double-process.
                    log.warning("no recent frames from the PLM mirror; not injecting")
                else:
                    # Same clock the buckets closed on, so suppression and
                    # rate limits cannot disagree with the fusion window.
                    injected = bool(self.injector.consider(event, now))
            self.misses.note(event, injected)
            if self.writer is not None:
                self.writer.write(event.to_dict())
            if self.on_event is not None:
                self.on_event(event)
            out.append(event)
        return out

    def run(self, stop: threading.Event, *, poll_ms: int = 500) -> None:
        """Loop until ``stop`` is set."""
        while not stop.is_set():
            cap = self.receiver.next_capture(poll_ms)
            if cap is not None:
                self.handle_capture(cap)
            self.drain()
        for event in self.fusion.flush():
            self.misses.note(event)
            if self.writer is not None:
                self.writer.write(event.to_dict())

    def stats(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "captures": self.captures,
            "packets": self.packets,
            "events": self.events,
            "receivers": sorted(self.receiver.last_seq),
            "capture_gaps": self.receiver.gaps,
            "captures_dropped": self.receiver.dropped,
        }
        d["plm_messages_seen"] = self.memory.frames
        d["undecodable_events"] = self.misses.undecodable
        if self.plm is not None:
            d["plm_frames"] = self.plm.frames
            d["plm_link_healthy"] = self.plm.healthy()
        if self.injector is not None:
            d["injector"] = self.injector.counters.to_dict()
        return d



__all__ = [
    "PLM_INJECT_TOPIC",
    "PLM_RX_TOPIC",
    "DeviceMisses",
    "MeshService",
    "MissTable",
    "PlmLink",
]
