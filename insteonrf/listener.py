"""Contract v1: what a listener publishes once it has decoded what it heard.

Every receiver (the T-Embed's ESPHome firmware, the rfcat dongle pod, the
RTL-SDR pod) decodes on its own and publishes **one message per
transmission** to ``insteon-rf/msg/<receiver>``. insteon-mqtt subscribes,
correlates the receivers against what the PLM heard, and decides what to
inject. See ``Doc/REARCHITECTURE.md``.

Payload::

    {"v": 1, "rx": "insteon-t-embed", "t": 1794759872.429,
     "frame": "0250294E522B9307270D02", "copies": 3, "hops_left": 2,
     "rssi": -78.5, "snr": null, "repaired": 0}

Rules every listener follows (this module is the reference; the ESPHome
decoder and insteon-mqtt's parser are tested against vectors made here):

1. Only messages whose packet CRC matches **and** whose frame-index counters
   run as the protocol requires, with possible hop fields, are published --
   :func:`insteonrf.plm.to_plm_bytes` with ``validate=True`` is the gate.
   The data CRC of an extended message is *not* required: the packet CRC
   already covers every data byte, and some devices leave it unset.
2. Hop copies are folded by the hop-insensitive key
   (:func:`insteonrf.plm.message_key`) for :data:`FOLD_WINDOW_S` of on-air
   time from the first copy.
3. The PLM's own transmissions are published too; the consumer drops them.
4. ``frame`` is in the PLM's layout: group broadcasts swap the RF slots.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .packet import Packet, parse_bits
from .plm import PlmFormatError, message_key, to_plm_bytes

CONTRACT_VERSION = 1
TOPIC_PREFIX = "insteon-rf/msg"
#: Four hop slots are 200 ms. A device re-sends a message 0.5-1.4 s later
#: (measured 2026-11-15, 110 of ~280 battery broadcasts); a window that ends
#: short of that keeps the retransmission a separate message on every
#: listener alike, rather than folded by one and not another.
FOLD_WINDOW_S = 0.35


@dataclass
class ListenerMessage:
    """One decoded transmission as one receiver heard it."""

    rx: str
    t: float
    frame: bytes
    hops_left: int
    copies: int = 1
    rssi: float | None = None
    snr: float | None = None
    repaired: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": CONTRACT_VERSION,
            "rx": self.rx,
            "t": round(self.t, 3),
            "frame": self.frame.hex().upper(),
            "copies": self.copies,
            "hops_left": self.hops_left,
            "rssi": None if self.rssi is None else round(self.rssi, 1),
            "snr": None if self.snr is None else round(self.snr, 1),
            "repaired": self.repaired,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ListenerMessage:
        if d.get("v") != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract version {d.get('v')!r}")
        return cls(
            rx=str(d["rx"]),
            t=float(d["t"]),
            frame=bytes.fromhex(d["frame"]),
            hops_left=int(d["hops_left"]),
            copies=int(d.get("copies", 1)),
            rssi=None if d.get("rssi") is None else float(d["rssi"]),
            snr=None if d.get("snr") is None else float(d["snr"]),
            repaired=int(d.get("repaired", 0)),
        )

    def topic(self, prefix: str = TOPIC_PREFIX) -> str:
        return f"{prefix}/{self.rx}"


def message_from_packet(pkt: Packet, rx: str, *, t: float | None = None) -> ListenerMessage | None:
    """The contract message for one decoded copy, or ``None`` if it fails rule 1."""
    try:
        frame = to_plm_bytes(pkt, validate=True)
    except PlmFormatError:
        return None
    when = t if t is not None else (pkt.timestamp if pkt.timestamp is not None else time.time())
    return ListenerMessage(
        rx=rx, t=when, frame=frame, hops_left=pkt.hops_left,
        rssi=pkt.rssi_dbm, snr=pkt.snr_db, repaired=pkt.corrected,
    )


class Folder:
    """Fold hop copies into one :class:`ListenerMessage` per transmission (rule 2).

    Feed every decoded packet to :meth:`add`; :meth:`pop_ready` returns the
    messages whose window has closed. Among the copies, the frame kept is the
    least repaired, then the one with the most hops left (closest to the
    original); ``t`` is the earliest copy, ``rssi``/``snr`` the best.
    """

    def __init__(self, rx: str, window_s: float = FOLD_WINDOW_S):
        self.rx = rx
        self.window_s = window_s
        self._pending: dict[tuple[Any, ...], ListenerMessage] = {}
        self._ready: list[ListenerMessage] = []

    def add(self, pkt: Packet, *, t: float | None = None) -> ListenerMessage | None:
        """Fold one copy in. Returns the (pending) message it joined, or ``None``."""
        msg = message_from_packet(pkt, self.rx, t=t)
        if msg is None:
            return None
        key = message_key(pkt)
        cur = self._pending.get(key)
        if cur is not None and msg.t - cur.t > self.window_s:
            # Same bytes, but too late to be a hop copy: a retry. Release the
            # old one first so ordering is preserved.
            self._ready.append(self._pending.pop(key))
            cur = None
        if cur is None:
            self._pending[key] = msg
            return msg
        cur.copies += 1
        if (msg.repaired, -msg.hops_left) < (cur.repaired, -cur.hops_left):
            cur.frame, cur.hops_left, cur.repaired = msg.frame, msg.hops_left, msg.repaired
        cur.t = min(cur.t, msg.t)
        cur.rssi = _best(cur.rssi, msg.rssi)
        cur.snr = _best(cur.snr, msg.snr)
        return cur

    def pop_ready(self, now: float | None = None) -> list[ListenerMessage]:
        """Messages whose fold window (from the first copy) has closed, oldest first."""
        if now is None:
            now = time.time()
        out, self._ready = self._ready, []
        for key, msg in list(self._pending.items()):
            if now - msg.t > self.window_s:
                out.append(msg)
                del self._pending[key]
        return sorted(out, key=lambda m: m.t)

    def flush(self) -> list[ListenerMessage]:
        out = self._ready + list(self._pending.values())
        self._ready, self._pending = [], {}
        return sorted(out, key=lambda m: m.t)

    def __len__(self) -> int:
        return len(self._pending)


def _best(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def capture_messages(bits: str, rx: str, t: float, *, sync_word: int | None = None,
                     rssi: float | None = None) -> list[ListenerMessage]:
    """Reference decode of one hard-decision capture with no repair.

    This is exactly what the T-Embed firmware does on the board: put back the
    start header the radio consumed, parse every packet in the capture, keep
    the ones that pass rule 1, and fold the hop copies within the capture.
    """
    from .radio.mqtt import with_sync_header

    folder = Folder(rx)
    for pkt in parse_bits(with_sync_header(bits, sync_word)):
        pkt.rssi_dbm = rssi
        folder.add(pkt, t=t)
    return folder.flush()


def iter_json(messages: Iterable[ListenerMessage]) -> Iterable[str]:
    for m in messages:
        yield m.to_json()
