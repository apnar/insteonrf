"""Long-running RF logger: JSON lines to a rotating file, optionally to MQTT.

Every Insteon message is repeated by the mesh with a decremented hop count,
so the air carries two to four copies of each packet. :class:`Deduper` folds
those into one record with a ``hops_seen`` list, which is what makes a day of
logs readable.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .packet import Packet

log = logging.getLogger(__name__)

#: Repeats of the same packet arrive within a few tens of ms.
DEDUPE_WINDOW_S = 0.25


def _dedupe_key(p: Packet) -> tuple[int, ...]:
    """The part of a packet that a mesh repeat does not change.

    A repeater decrements the hops-left field of the flags byte, which also
    changes the packet CRC computed over it — so the key is the bytes *before*
    the CRC, with hops-left masked out of the flags. Everything from the CRC
    onwards is dropped rather than masked because the trailing pad bytes are
    routinely cut short by whatever ends the receive block, and two copies of
    one message often arrive with different amounts of padding.
    """
    data = list(p.data[: p.crc_index] if len(p.data) > p.crc_index else p.data)
    data[0] &= ~0x0C
    return tuple(data)


class Deduper:
    """Collapse mesh repeats of one packet into a single record.

    Records are held for ``window_s`` after the last sighting and handed over
    by :meth:`pop_ready`, so what gets logged carries the final ``repeats`` and
    ``hops_seen`` rather than only the first copy that arrived.
    """

    def __init__(self, window_s: float = DEDUPE_WINDOW_S):
        self.window_s = window_s
        self._pending: dict[tuple[int, ...], tuple[float, dict[str, Any]]] = {}

    def add(self, p: Packet) -> None:
        """Fold a packet into the pending records."""
        now = p.timestamp if p.timestamp is not None else time.time()
        key = _dedupe_key(p)
        hit = self._pending.get(key)
        if hit is not None:
            _, rec = hit
            rec["repeats"] += 1
            if p.hops_left not in rec["hops_seen"]:
                rec["hops_seen"].append(p.hops_left)
        else:
            rec = p.to_dict()
            rec["repeats"] = 1
            rec["hops_seen"] = [p.hops_left]
        self._pending[key] = (now, rec)

    def pop_ready(self, now: float | None = None) -> list[dict[str, Any]]:
        """Return the records whose dedupe window has closed."""
        if now is None:
            now = time.time()
        out = []
        for key, (last, rec) in list(self._pending.items()):
            if now - last > self.window_s:
                out.append(rec)
                del self._pending[key]
        return out

    def flush(self) -> list[dict[str, Any]]:
        """Return every pending record and forget them (use at shutdown)."""
        out = [rec for _, rec in self._pending.values()]
        self._pending.clear()
        return out

    def __len__(self) -> int:
        return len(self._pending)


class JsonlWriter:
    """Append JSON lines to a file, rotating it at ``max_bytes``."""

    def __init__(self, path: str | Path, *, max_bytes: int = 32 << 20, backups: int = 5):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()
        if self.max_bytes and self._fh.tell() >= self.max_bytes:
            self.rotate()

    def rotate(self) -> None:
        self._fh.close()
        for n in range(self.backups - 1, 0, -1):
            src, dst = Path(f"{self.path}.{n}"), Path(f"{self.path}.{n + 1}")
            if src.exists():
                src.replace(dst)
        if self.backups:
            self.path.replace(Path(f"{self.path}.1"))
        self._fh = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._fh.close()


class MqttPublisher:
    """Publish JSON to ``<topic>`` (and ``<topic>/<address>``).

    Nothing accumulates on the broker when no one is subscribed: these go out
    at QoS 0, unretained, so the broker drops them. What *does* cost something
    is the broker's own log — a mosquitto running ``log_type debug`` writes a
    line per publish — so publishing a per-packet stream that nobody reads is
    pure overhead. Use :meth:`publish_alert` and ``alerts_only`` when the file
    is the record and MQTT is only there to wake something up.
    """

    def __init__(self, host: str, port: int = 1883, topic: str = "insteon-rf",
                 *, username: str | None = None, password: str | None = None,
                 client_id: str = "insteon-rf", per_device: bool = True, qos: int = 0,
                 alerts_only: bool = False, alert_qos: int = 1,
                 alert_retain: bool = False):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:  # pragma: no cover - optional dependency
            raise SystemExit("paho-mqtt is not installed: pip install 'insteonrf[mqtt]'") from err
        self.topic = topic.rstrip("/")
        self.per_device = per_device
        self.qos = qos
        #: Publish only triggers, not every packet.
        self.alerts_only = alerts_only
        self.alert_qos = alert_qos
        #: Retaining an alert means a consumer that connects later still sees
        #: the last one — at the cost of re-firing on every reconnect.
        self.alert_retain = alert_retain
        self.published = 0
        self.alerts = 0
        self.captures = 0
        # paho 2.x deprecates the v1 callback API; ask for v2 where it exists.
        api = getattr(mqtt, "CallbackAPIVersion", None)
        self.client = (mqtt.Client(api.VERSION2, client_id=client_id) if api is not None
                       else mqtt.Client(client_id=client_id))
        if username:
            self.client.username_pw_set(username, password)
        self.client.connect_async(host, port)
        self.client.loop_start()
        log.info("publishing to mqtt://%s:%d/%s", host, port, self.topic)

    def publish(self, record: dict[str, Any]) -> None:
        """Publish one packet record, unless configured for alerts only."""
        if self.alerts_only:
            return
        payload = json.dumps(record, separators=(",", ":"))
        self.client.publish(self.topic, payload, qos=self.qos)
        self.published += 1
        if self.per_device:
            who = record.get("from") or record.get("to")
            if who:
                self.client.publish(f"{self.topic}/{who}", payload, qos=self.qos)

    def publish_capture(self, bits: str, *, receiver: str, timestamp: float,
                        rssi_dbm: float | None = None, seq: int = 0,
                        topic: str | None = None) -> None:
        """Publish a raw burst in the listener-board capture format.

        This is what lets the rfcat dongle act as a member of the listener
        mesh: ``insteon-rf mesh`` consumes ``<prefix>/rx/<node>`` and does not
        care whether the bytes came from an ESP32 or from USB. It is also the
        only receiver that can produce soft decisions, so it stays useful
        after the boards arrive.
        """
        import base64

        from .radio.mqtt import bytes_from_bits

        payload = {
            "n": receiver,
            "seq": seq,
            "t": int(timestamp * 1000),
            "rssi": rssi_dbm,
            "len": (len(bits) + 7) // 8,
            "b": base64.b64encode(bytes_from_bits(bits)).decode("ascii"),
        }
        self.client.publish(topic or f"{self.topic}/rx/{receiver}",
                            json.dumps(payload, separators=(",", ":")), qos=0)
        self.captures += 1

    def publish_alert(self, alert: dict[str, Any]) -> None:
        """Publish a trigger to ``<topic>/alert``, at a higher QoS than packets."""
        self.client.publish(f"{self.topic}/alert", json.dumps(alert, separators=(",", ":")),
                            qos=self.alert_qos, retain=self.alert_retain)
        self.alerts += 1

    def close(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception as err:  # pragma: no cover - best effort
            log.debug("mqtt disconnect: %r", err)


def records(packets: Iterable[Packet], *, dedupe: bool = True,
            window_s: float = DEDUPE_WINDOW_S) -> Iterator[dict[str, Any]]:
    """Turn packets into JSON-ready records, optionally deduping mesh repeats."""
    if not dedupe:
        for p in packets:
            yield p.to_dict()
        return
    dd = Deduper(window_s)
    for p in packets:
        dd.add(p)
        yield from dd.pop_ready(p.timestamp)
    yield from dd.flush()
