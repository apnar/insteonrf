"""Receive captures from the ESPHome listener boards over MQTT.

Each board publishes one message per accepted capture to
``<prefix>/rx/<node>``, carrying the raw on-air bytes exactly as the SX1262's
FIFO delivered them:

.. code-block:: json

    {"n": "insteon-rf-up", "seq": 48213, "t": 1789412345678,
     "us": 91234567, "rssi": -87.5, "len": 128, "b": "<base64>"}

``b`` is base64 rather than hex because it is a third smaller, and the
payload is the bulk of the traffic. The bytes expand to an ASCII bit string
MSB first — the order the radio shifts bits into the FIFO — which is the
pipeline contract the rest of the package already speaks.

**``b`` is the FIFO content, which starts *after* the sync word.** A radio
consumes the pattern it synchronised on, so the first packet in every capture
arrives without its start header, and the parser would skip it — measured on
the first Heltec: three of four captures decoded truncated or not at all
because only the *next* packet in the buffer still had a header. The host
puts the header back (:func:`with_sync_header`), exactly as the rfcat path
does for the CC1111 in ``RfcatRadio.receive_bits``. ``sw`` names the sync
word the board matched on, so a board running the inverted word gets the
matching header prepended; without it the default on-air header is used.

Only ``b`` is required. ``n`` falls back to the last topic segment, which is
where the node name really lives; the rest is metadata.

**``s`` carries soft decisions**, when the receiver has them: base64 of one
signed byte per bit of ``b`` (pad bits included), ``+127`` a clean ``1``,
``-127`` a clean ``0``, values near zero a symbol the detector could not
tell. Only an I/Q receiver (the ``rtlsdr``/``hackrf`` backends) can produce
this; the SX1262 and the CC1111 are hardware demodulators and send hard
bits, so ``s`` is absent from their captures and :class:`Capture.soft` is
``None``. A capture whose ``s`` does not match ``b`` in length is kept as
hard bits — the bits are still good, the confidence is not. ``snr`` (dB,
symbol SNR the demodulator measured) is optional alongside it.

This satisfies :class:`~insteonrf.radio.RadioBackend` so ``insteon-rf
monitor`` and friends work against the mesh unchanged, but a caller that
wants per-receiver RSSI should use :meth:`MqttReceiver.iter_captures`
instead — :meth:`receive_bits` can only return ``(time, bits)`` and throws
the receiver identity away, which is the whole point of having several.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import queue
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..packet import START_HEADER, START_HEADER_INV

log = logging.getLogger(__name__)

#: Default topic prefix. Deliberately not ``insteon/``, which insteon-mqtt owns.
DEFAULT_PREFIX = "insteon-rf"
#: Drop a capture whose payload is larger than this (a board is misbehaving).
MAX_CAPTURE_BYTES = 4096
#: One soft decision is a signed byte; this magnitude is "a clean symbol".
SOFT_SCALE = 127


def bits_from_bytes(data: bytes) -> str:
    """Expand bytes to an ASCII bit string, most significant bit first."""
    return "".join(format(b, "08b") for b in data)


def bytes_from_bits(bits: str) -> bytes:
    """Inverse of :func:`bits_from_bytes`, padding the last byte with zeros."""
    pad = (-len(bits)) % 8
    padded = bits + "0" * pad
    return bytes(int(padded[i : i + 8], 2) for i in range(0, len(padded), 8))


def soft_to_bytes(soft: np.ndarray[Any, Any]) -> bytes:
    """Quantise per-symbol confidence to signed bytes (``±SOFT_SCALE`` = clean)."""
    q = np.clip(np.rint(np.asarray(soft, dtype=np.float32) * SOFT_SCALE), -SOFT_SCALE, SOFT_SCALE)
    return q.astype(np.int8).tobytes()


def soft_from_bytes(data: bytes) -> np.ndarray[Any, Any]:
    """Inverse of :func:`soft_to_bytes`: float32 confidence in ``[-1, 1]``."""
    return np.frombuffer(data, dtype=np.int8).astype(np.float32) / SOFT_SCALE


def hard_soft(bits: str) -> np.ndarray[Any, Any]:
    """``±1`` per bit — the confidence of a bit that is simply known."""
    a = np.frombuffer(bits.encode("ascii"), dtype=np.uint8)
    return np.where(a == ord("1"), 1.0, -1.0).astype(np.float32)


def with_sync_header(bits: str, sync_word: int | None = None) -> str:
    """Put back the start header the radio consumed while synchronising.

    The low 16 bits of the sync word *are* the on-air start header (the
    upper bits are preamble), so prepending them restores a stream the
    parser can find the first packet in. Prepending the header in the same
    polarity the board matched on keeps the whole stream self-consistent, so
    a board flipped to the inverted sync word needs nothing else changed.

    A stream that already begins with a header is left alone: that is how an
    older publisher (the dongle path before it stripped its own prefix)
    presents captures, and a second header would put a junk packet in front
    of the real one.
    """
    if bits.startswith(START_HEADER) or bits.startswith(START_HEADER_INV):
        return bits
    header = f"{sync_word & 0xFFFF:016b}" if sync_word is not None else START_HEADER_INV
    return header + bits


@dataclass
class Capture:
    """One board's recording of one burst."""

    receiver: str
    bits: str
    timestamp: float
    rssi_dbm: float | None = None
    seq: int | None = None
    #: Microseconds since that board booted, at sync detect. High resolution
    #: *within* a board, meaningless between boards.
    micros: int | None = None
    #: The sync word the board matched on, when it said.
    sync_word: int | None = None
    #: Per-symbol signed confidence aligned with ``bits`` (header included,
    #: at full confidence), or ``None`` for a hard-bit receiver.
    soft: np.ndarray[Any, Any] | None = field(default=None, repr=False)
    #: Symbol SNR the demodulator measured, when it said.
    snr_db: float | None = None

    @classmethod
    def from_payload(cls, topic: str, payload: bytes) -> Capture | None:
        """Parse one MQTT message, or ``None`` if it is not usable.

        Boards are the least trusted part of this system — a wedged one can
        send anything — so every field is checked rather than assumed.
        """
        try:
            rec = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            log.warning("undecodable capture on %s: %r", topic, err)
            return None
        if not isinstance(rec, dict):
            log.warning("capture on %s is not an object", topic)
            return None

        blob = rec.get("b")
        if not isinstance(blob, str):
            log.warning("capture on %s has no 'b' field", topic)
            return None
        try:
            raw = base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError) as err:
            log.warning("capture on %s is not valid base64: %r", topic, err)
            return None
        if not raw or len(raw) > MAX_CAPTURE_BYTES:
            log.warning("capture on %s is %d bytes; dropping", topic, len(raw))
            return None

        name = rec.get("n")
        if not isinstance(name, str) or not name:
            name = topic.rsplit("/", 1)[-1]

        ts = rec.get("t")
        # Boards send epoch milliseconds. A board whose SNTP has not synced
        # yet sends something implausible, so fall back to arrival time
        # rather than filing the capture in 1970.
        when = time.time()
        if isinstance(ts, (int, float)) and ts > 1_000_000_000_000:
            candidate = float(ts) / 1000.0
            if abs(candidate - when) < 3600:
                when = candidate

        rssi = rec.get("rssi")
        seq = rec.get("seq")
        micros = rec.get("us")

        sw = rec.get("sw")
        sync_word: int | None = None
        if isinstance(sw, int):
            sync_word = sw
        elif isinstance(sw, str):
            try:
                sync_word = int(sw, 16)
            except ValueError:
                log.warning("capture on %s has an unparsable sw %r", topic, sw)

        fifo = bits_from_bytes(raw)
        bits = with_sync_header(fifo, sync_word)

        soft: np.ndarray[Any, Any] | None = None
        blob_s = rec.get("s")
        if isinstance(blob_s, str):
            try:
                raw_s = base64.b64decode(blob_s, validate=True)
            except (binascii.Error, ValueError) as err:
                log.warning("capture on %s has soft decisions that are not base64: %r", topic, err)
                raw_s = b""
            if len(raw_s) == len(fifo):
                # The header the host put back is known, so it is certain.
                prefix = hard_soft(bits[: len(bits) - len(fifo)])
                soft = np.concatenate([prefix, soft_from_bytes(raw_s)])
            elif raw_s:
                log.warning("capture on %s: %d soft values for %d bits; using hard bits",
                            topic, len(raw_s), len(fifo))

        snr = rec.get("snr")

        return cls(
            receiver=name,
            bits=bits,
            timestamp=when,
            rssi_dbm=float(rssi) if isinstance(rssi, (int, float)) else None,
            seq=int(seq) if isinstance(seq, int) else None,
            micros=int(micros) if isinstance(micros, int) else None,
            sync_word=sync_word,
            soft=soft,
            snr_db=float(snr) if isinstance(snr, (int, float)) else None,
        )


class MqttReceiver:
    """Subscribe to board captures and hand them over one at a time."""

    name = "mqtt"

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        prefix: str = DEFAULT_PREFIX,
        username: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        maxsize: int = 4096,
    ):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:  # pragma: no cover - optional dependency
            raise SystemExit(
                "paho-mqtt is not installed: pip install 'insteonrf[mqtt]'"
            ) from err

        self.exhausted = False
        self.prefix = prefix.rstrip("/")
        self.topic = f"{self.prefix}/rx/+"
        self._q: queue.Queue[Capture] = queue.Queue(maxsize=maxsize)
        #: Last sequence number per board, for spotting gaps.
        self.last_seq: dict[str, int] = {}
        self.received = 0
        self.dropped = 0
        self.gaps = 0

        # A fixed client id means two instances evict each other in a loop,
        # each reconnect re-subscribing -- easy to mistake for a broker fault.
        if client_id is None:
            client_id = "insteon-rf-mesh-" + uuid.uuid4().hex[:8]
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
        log.info("listening for captures on mqtt://%s:%d/%s", host, port, self.topic)

    # -- paho callbacks ---------------------------------------------------
    def _on_connect(self, client: Any, *_: Any, **__: Any) -> None:
        client.subscribe(self.topic, qos=0)
        log.info("subscribed to %s", self.topic)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        cap = Capture.from_payload(message.topic, message.payload)
        if cap is None:
            self.dropped += 1
            return
        if cap.seq is not None:
            prev = self.last_seq.get(cap.receiver)
            # A board reboot resets seq, which is not a gap.
            if prev is not None and cap.seq > prev + 1:
                self.gaps += 1
                log.debug("%s skipped %d captures", cap.receiver, cap.seq - prev - 1)
            self.last_seq[cap.receiver] = cap.seq
        try:
            self._q.put_nowait(cap)
            self.received += 1
        except queue.Full:
            self.dropped += 1
            log.warning("capture queue is full; dropping from %s", cap.receiver)

    # -- rich API ---------------------------------------------------------
    def next_capture(self, timeout_ms: int = 2000) -> Capture | None:
        try:
            return self._q.get(timeout=max(timeout_ms, 1) / 1000.0)
        except queue.Empty:
            return None

    def iter_captures(self, timeout_ms: int = 2000) -> Iterator[Capture]:
        while True:
            cap = self.next_capture(timeout_ms)
            if cap is not None:
                yield cap

    # -- RadioBackend -----------------------------------------------------
    def configure_rx(self, **_: Any) -> None:
        """Nothing to configure: the boards own their radio settings."""

    def configure_tx(self) -> None:
        raise NotImplementedError("the mesh is receive-only; the PLM owns the air")

    def transmit_bits(self, bits: str, **_: Any) -> None:
        raise NotImplementedError("the mesh is receive-only; the PLM owns the air")

    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
        cap = self.next_capture(timeout_ms)
        return None if cap is None else (cap.timestamp, cap.bits)

    def iter_bits(self, timeout_ms: int = 2000) -> Iterator[tuple[float, str]]:
        for cap in self.iter_captures(timeout_ms):
            yield cap.timestamp, cap.bits

    def close(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception as err:  # pragma: no cover - best effort
            log.debug("mqtt disconnect: %r", err)

    def __enter__(self) -> MqttReceiver:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_PREFIX",
    "MAX_CAPTURE_BYTES",
    "Capture",
    "MqttReceiver",
    "bits_from_bytes",
    "bytes_from_bits",
    "with_sync_header",
]
