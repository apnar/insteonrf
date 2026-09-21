"""Fuse sightings of one Insteon message from several receivers.

The mesh (``Doc/MESH-PLAN.md``) has every board report independently to MQTT
and does all the combining here. That choice removes any need for clock
synchronisation between boards, because the thing we actually want to know —
which copy is closest to the transmitter — is carried *in the packet*: the
flags byte holds hops-left, and the copy with the most hops left has travelled
least. Timestamps are used only to decide which sightings belong together.

Four kinds of duplication arrive, and conflating them loses information:

===========================  =============================  ==================
Kind                         Signature                      Wanted
===========================  =============================  ==================
Mesh hop repeat              same bytes, hops decremented   fold
Several receivers, one hop   same bytes, same hops          fold
Several receivers, diff hop  same bytes, diff hops          fold
Sender retransmission        same bytes, hops back at max,  **keep separate**
                             seconds later
===========================  =============================  ==================

The first three fold onto one key (:func:`~insteonrf.plm.message_key`, which
masks hops-left). The fourth must not: an Insteon retry after no ACK, or a
second button press, is a real event. It is told apart by arriving after the
bucket for that key has already closed, and comes out with ``repeat_index``
set rather than being silently absorbed.

:func:`combine` is where several receivers become more than one. Thermal
noise at different locations is independent, so voting the bits of several
failed copies recovers packets no single receiver got. A hardware
demodulator (SX1262, CC1111) contributes its bits as ``±1`` votes; an I/Q
receiver contributes its per-symbol confidence (:attr:`Sighting.soft`), so a
symbol it was unsure of is outvoted by one a hard receiver was sure of, and
the least confident positions are the first to be tried when the vote still
fails the CRC. One soft copy on its own is enough to attempt repair; hard
copies need at least two. Every combined candidate still has to satisfy
*both* CRC and the frame-index counters — an 8-bit CRC accepts one random
candidate in 256, so combining without the index check would manufacture
plausible-looking messages, and these get fed to a real protocol stack.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np

from .packet import (
    EXT_LEN,
    FLAG_EXT,
    FRAME_BITS,
    MARKER_OFFSET,
    STD_LEN,
    Packet,
    decode_frames,
    find_headers,
    indexes_ok,
    parse_bits,
)
from .plm import message_key

#: Copies of one transmission land inside this window.
WINDOW_S = 0.6
#: Each further copy extends the window by this much...
EXTEND_S = 0.2
#: ...but never past this, so a repeat chain cannot keep a bucket open forever.
MAX_WINDOW_S = 2.0
#: How long a closed key is remembered, for numbering retransmissions.
REPEAT_MEMORY_S = 60.0

#: Most bits to flip when combining copies that disagree.
MAX_COMBINE_FLIPS = 2
#: Refuse to even try if the copies disagree in more places than this: the
#: candidate space grows fast and a wide disagreement means one copy is junk.
MAX_DISAGREEMENTS = 12
#: Two bit strings are only comparable if they overlap by at least this much.
MIN_COMPARE_BITS = 200


@dataclass
class Sighting:
    """One receiver's decode of one transmission.

    ``bits`` is the receiver's whole capture from this packet's start header
    to the end of the block, polarity-normalised. It is *not*
    ``packet.bits``, and the difference is what makes combining possible:
    ``decode_frames`` stops at the first damaged Manchester pair, so a
    damaged copy's ``packet.bits`` is truncated at the damage and throws away
    the very region the other receivers could have voted on. The full capture
    keeps it. Build sightings with :func:`sightings_from_capture` and this is
    handled.
    """

    packet: Packet
    receiver: str
    rssi_dbm: float | None = None
    timestamp: float | None = None
    bits: str = ""
    #: Symbol SNR in dB, from an I/Q receiver; ``None`` for hard bits. Kept
    #: per receiver rather than per event because comparing receivers is the
    #: point of having more than one, and this is the number that says how
    #: much margin each of them had on the same transmission.
    snr_db: float | None = None
    #: Per-symbol signed confidence aligned with ``bits`` (positive = ``1``,
    #: ``±1`` a clean symbol), from an I/Q receiver; ``None`` for hard bits.
    soft: np.ndarray[Any, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.bits:
            self.bits = self.packet.bits
        if self.soft is not None and len(self.soft) < len(self.bits):
            self.soft = None  # misaligned confidence is worse than none

    def votes(self) -> np.ndarray[Any, Any]:
        """This copy's contribution to a vote: its confidence, or ``±1``."""
        if self.soft is not None:
            return np.asarray(self.soft[: len(self.bits)], dtype=np.float32)
        a = np.frombuffer(self.bits.encode("ascii"), dtype=np.uint8)
        return np.where(a == ord("1"), 1.0, -1.0).astype(np.float32)

    @property
    def when(self) -> float:
        if self.timestamp is not None:
            return self.timestamp
        if self.packet.timestamp is not None:
            return self.packet.timestamp
        return time.time()


def sightings_from_capture(
    capture: str,
    receiver: str,
    *,
    rssi_dbm: float | None = None,
    timestamp: float | None = None,
    min_bytes: int = 4,
    soft: np.ndarray[Any, Any] | None = None,
    snr_db: float | None = None,
) -> list[Sighting]:
    """Every packet in one receiver's capture, each with its aligned bits.

    A capture is longer than one packet (the SX1262 records a fixed block, and
    Insteon repeats each message), so one block routinely holds a packet plus
    the start of the next hop. Each returned sighting carries the capture from
    its own header onwards, which is the aligned substrate
    :func:`combine` votes over.

    ``soft`` is per-symbol confidence aligned with ``capture`` (no surrounding
    whitespace, then). The capture is polarity-normalised here, and the
    confidence follows: a flipped stream means every sign flips.
    """
    stream = capture.strip()
    bits, offsets = find_headers(stream)
    conf: np.ndarray[Any, Any] | None = None
    if soft is not None and len(soft) >= len(stream):
        conf = np.asarray(soft[: len(stream)], dtype=np.float32)
        if bits != stream:
            conf = -conf
    out = []
    for pos in offsets:
        data, idx = decode_frames(bits, pos + MARKER_OFFSET)
        if len(data) < min_bytes:
            continue
        end = pos + MARKER_OFFSET + FRAME_BITS * len(data)
        pkt = Packet(data, bits[pos:end], timestamp,
                     complete=bool(idx) and idx[-1] == 0)
        pkt.index_ok = indexes_ok(data, idx)
        pkt.rssi_dbm = rssi_dbm
        out.append(Sighting(pkt, receiver, rssi_dbm, timestamp, bits[pos:],
                            snr_db=snr_db,
                            soft=None if conf is None else conf[pos:]))
    return out


@dataclass
class ReceiverView:
    """What one receiver contributed to a fused event."""

    copies: int = 0
    crc_ok: bool = False
    rssi_dbm: float | None = None
    best_hops_left: int = -1
    #: Best symbol SNR this receiver had on this message, if it measures one.
    snr_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "copies": self.copies,
            "crc_ok": self.crc_ok,
            "rssi_dbm": self.rssi_dbm,
            "snr_db": self.snr_db,
            "hops_left": self.best_hops_left if self.best_hops_left >= 0 else None,
        }


@dataclass
class FusedEvent:
    """One transmission, as seen by every receiver that heard it."""

    key: tuple[Any, ...]
    packet: Packet
    first_seen: float
    last_seen: float
    hops_left_min: int
    hops_left_max: int
    receivers: dict[str, ReceiverView] = field(default_factory=dict)
    #: Receiver reporting the copy that travelled least, tie-broken by RSSI.
    #: For an all-on event this single field answers "which room".
    closest: str | None = None
    #: True when the packet came out of :meth:`Fusion.combine` rather than
    #: from any single receiver intact.
    combined: bool = False
    #: How many sightings the combined decode drew on.
    combined_from: int = 0
    #: 0 for the first transmission of these bytes, 1.. for retransmissions.
    repeat_index: int = 0
    #: Set by the injector once it knows whether the PLM also heard it.
    plm_saw_it: bool | None = None

    @property
    def hops_used(self) -> int:
        return self.packet.max_hops - self.hops_left_min

    @property
    def heard_by(self) -> int:
        return len(self.receivers)

    def to_dict(self) -> dict[str, Any]:
        d = self.packet.to_dict()
        d.update(
            {
                "hops_left_min": self.hops_left_min,
                "hops_left_max": self.hops_left_max,
                "hops_used": self.hops_used,
                "heard_by": {n: v.to_dict() for n, v in self.receivers.items()},
                "receiver_count": self.heard_by,
                "closest": self.closest,
                "combined": self.combined,
                "combined_from": self.combined_from,
                "repeat_index": self.repeat_index,
                "plm_saw_it": self.plm_saw_it,
                "first_seen": self.first_seen,
                "last_seen": self.last_seen,
            }
        )
        return d


# --------------------------------------------------------------------------- combining


def hamming(a: str, b: str, limit: int | None = None) -> int | None:
    """Bit distance over the common prefix, or ``None`` if not comparable.

    Stops early once ``limit`` is exceeded, which is what makes it cheap
    enough to compare a new sighting against every open bucket.
    """
    n = min(len(a), len(b))
    if n < MIN_COMPARE_BITS:
        return None
    d = 0
    for i in range(n):
        if a[i] != b[i]:
            d += 1
            if limit is not None and d > limit:
                return d
    return d


@dataclass
class Vote:
    """The outcome of voting aligned copies: bits, and how sure each one is."""

    bits: str
    #: Positions where the copies' hard decisions disagreed.
    disagree: list[int]
    #: Per position, the mean signed vote — ``|margin|`` near 1 is unanimous
    #: and confident, near 0 is a coin toss.
    margin: np.ndarray[Any, Any]


def _vote(sightings: list[Sighting]) -> Vote | None:
    """Vote the aligned copies; needs two, or one that carries confidence.

    ``Packet.bits`` always starts at the start header and in normalised
    polarity (see :func:`~insteonrf.packet.find_headers`), so copies of one
    transmission are aligned without any extra work.

    Voting is per position over whichever copies reach that position, not over
    the shortest copy. That matters: a corrupted Manchester pair makes
    ``decode_frames`` stop early, so a damaged copy is often *shorter*, and
    truncating the vote to it would throw away the CRC region and guarantee
    failure. Each copy votes with its confidence — ``±1`` from a hard
    receiver, ``[-1, 1]`` from an I/Q one — so a soft copy's doubtful symbol
    is outweighed by a hard copy's certain one.
    """
    runs = [s for s in sightings if s.bits]
    if not runs or (len(runs) < 2 and runs[0].soft is None):
        return None
    width = max(len(s.bits) for s in runs)
    if width == 0:
        return None
    total = np.zeros(width, dtype=np.float32)
    count = np.zeros(width, dtype=np.int32)
    ones = np.zeros(width, dtype=np.int32)
    first = np.zeros(width, dtype=np.int8)
    for s in runs:
        v = s.votes()
        n = len(v)
        total[:n] += v
        count[:n] += 1
        hard = (v > 0).astype(np.int32)
        ones[:n] += hard
        first[:n] = np.where(count[:n] == 1, hard, first[:n])
    margin = total / np.maximum(count, 1)
    decided = np.where(margin > 0, 1, np.where(margin < 0, 0, first)).astype(np.int8)
    bits = "".join("1" if b else "0" for b in decided)
    disagree = [int(i) for i in np.flatnonzero((ones > 0) & (ones < count))]
    return Vote(bits, disagree, margin)


def _packet_extent(bits: str) -> int:
    """How many bits from the header a packet of these bits occupies.

    Read from the flags byte when the first frame survived; a standard
    packet when it did not — the guess that keeps repair inside the region
    where a wrong bit can matter.
    """
    data, _ = decode_frames(bits, MARKER_OFFSET)
    frames = EXT_LEN if data and data[0] & FLAG_EXT else STD_LEN
    return MARKER_OFFSET + FRAME_BITS * frames


def _accept(bits: str, timestamp: float | None) -> Packet | None:
    """Parse a candidate and accept it only if it is unambiguously a packet."""
    for pkt in parse_bits(bits, timestamp):
        if pkt.crc_ok is True and pkt.index_ok is not False:
            if pkt.extended and pkt.ext_crc_ok is False:
                continue
            return pkt
    return None


def combine(sightings: list[Sighting]) -> tuple[Packet, int] | None:
    """Try to recover a packet from copies where no single one is intact.

    Vote first; if that still fails the CRC, flip small subsets of the
    suspect positions. With hard copies only, the suspects are where the
    receivers disagreed — direct evidence that one of them is wrong. With a
    soft copy in the mix, the suspects are the positions the weighted vote
    was least sure of, which covers both a disagreement and a symbol the
    I/Q receiver itself flagged as doubtful; one soft copy alone is enough
    to try.

    Returns ``(packet, n_sightings)`` or ``None``.
    """
    voted = _vote(sightings)
    if voted is None:
        return None
    bits, disagree = voted.bits, voted.disagree

    pkt = _accept(bits, sightings[0].when)
    if pkt is not None:
        return pkt, len(sightings)

    if len(disagree) > MAX_DISAGREEMENTS:
        return None

    soft_copies = any(s.soft is not None for s in sightings)
    if soft_copies:
        # With confidence available the suspects are the positions the vote
        # was least sure of, inside the packet, tried least-sure first. A
        # hard disagreement is always among them: its margin is small.
        extent = min(_packet_extent(bits), len(bits))
        order = np.argsort(np.abs(voted.margin[:extent]), kind="stable")
        suspects = [int(i) for i in order[:MAX_DISAGREEMENTS]]
        for i in disagree:
            if i not in suspects:
                suspects.append(i)
        suspects = suspects[:MAX_DISAGREEMENTS]
    else:
        suspects = disagree
    if not suspects:
        return None

    flip = list(bits)
    for count in range(1, MAX_COMBINE_FLIPS + 1):
        for positions in combinations(suspects, count):
            for i in positions:
                flip[i] = "0" if flip[i] == "1" else "1"
            pkt = _accept("".join(flip), sightings[0].when)
            for i in positions:  # restore
                flip[i] = "0" if flip[i] == "1" else "1"
            if pkt is not None:
                return pkt, len(sightings)
    return None


# --------------------------------------------------------------------------- fusion


class Fusion:
    """Collect sightings into :class:`FusedEvent` objects.

    Buckets stay open for ``window_s`` after the last sighting, extended by
    ``extend_s`` each time and capped at ``max_window_s``. :meth:`pop_ready`
    hands over the ones that have closed.
    """

    def __init__(
        self,
        window_s: float = WINDOW_S,
        *,
        extend_s: float = EXTEND_S,
        max_window_s: float = MAX_WINDOW_S,
        repeat_memory_s: float = REPEAT_MEMORY_S,
        allow_combine: bool = True,
    ):
        self.window_s = window_s
        self.extend_s = extend_s
        self.max_window_s = max_window_s
        self.repeat_memory_s = repeat_memory_s
        self.allow_combine = allow_combine
        self._open: dict[tuple[Any, ...], _Bucket] = {}
        self._closed: dict[tuple[Any, ...], tuple[float, int]] = {}

    # -- input ------------------------------------------------------------
    def add(self, sighting: Sighting) -> None:
        """Fold one sighting into the open buckets."""
        key = message_key(sighting.packet)
        bucket = self._open.get(key)
        if bucket is not None:
            bucket.add(sighting)
            return

        # An exact key only groups copies that decoded identically, which is
        # precisely what damaged copies do not do: one wrong bit changes the
        # bytes and therefore the key. Copies that need combining would never
        # meet. So fall back to bit similarity -- but narrowly.
        #
        # Similarity is only used when at least one side is not a confirmed
        # packet, because two genuinely different messages from one device can
        # be a handful of bits apart (cmd2 0x01 vs 0x02 is four on-air bits
        # plus the CRC) and merging those would be wrong. A copy whose CRC
        # passed is taken at its word.
        if self.allow_combine:
            near = self._nearest(sighting)
            if near is not None:
                near.add(sighting)
                return

        self._open[key] = _Bucket(key, sighting, self)

    def _nearest(self, sighting: Sighting) -> _Bucket | None:
        """The open bucket whose bits are closest, if close enough to be the
        same transmission."""
        incoming_good = sighting.packet.crc_ok is True
        best: _Bucket | None = None
        best_d = MAX_DISAGREEMENTS + 1
        for bucket in self._open.values():
            if incoming_good and bucket.has_good:
                continue  # two confirmed packets: trust their bytes
            d = hamming(sighting.bits, bucket.ref_bits, MAX_DISAGREEMENTS)
            if d is not None and d <= MAX_DISAGREEMENTS and d < best_d:
                best, best_d = bucket, d
        return best

    def add_packet(
        self,
        packet: Packet,
        receiver: str,
        *,
        rssi_dbm: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        self.add(Sighting(packet, receiver, rssi_dbm, timestamp))

    # -- output -----------------------------------------------------------
    def pop_ready(self, now: float | None = None) -> list[FusedEvent]:
        """Return the events whose bucket has closed."""
        if now is None:
            now = time.time()
        self._forget_old(now)
        out = []
        for key, bucket in list(self._open.items()):
            if bucket.is_closed(now):
                del self._open[key]
                out.append(self._finish(bucket, now))
        out.sort(key=lambda e: e.first_seen)
        return out

    def flush(self, now: float | None = None) -> list[FusedEvent]:
        """Close every open bucket (use at shutdown)."""
        if now is None:
            now = time.time()
        out = [self._finish(b, now) for b in self._open.values()]
        self._open.clear()
        out.sort(key=lambda e: e.first_seen)
        return out

    def __len__(self) -> int:
        return len(self._open)

    # -- internals --------------------------------------------------------
    def _forget_old(self, now: float) -> None:
        for key, (when, _) in list(self._closed.items()):
            if now - when > self.repeat_memory_s:
                del self._closed[key]

    def _finish(self, bucket: _Bucket, now: float) -> FusedEvent:
        event = bucket.build(self.allow_combine)
        # A bucket opened by a damaged copy carries that copy's (wrong) key.
        # Once combining has recovered the real bytes, re-key the event so
        # repeat numbering counts the actual message.
        if event.combined:
            event.key = message_key(event.packet)
        seen = self._closed.get(event.key)
        event.repeat_index = 0 if seen is None else seen[1] + 1
        self._closed[event.key] = (now, event.repeat_index)
        return event


class _Bucket:
    """Sightings of one message, gathered until the window closes."""

    def __init__(self, key: tuple[Any, ...], first: Sighting, cfg: Fusion):
        self.key = key
        self.cfg = cfg
        self.sightings: list[Sighting] = []
        self.opened = first.when
        self.last = first.when
        #: Bits to compare later arrivals against; the longest seen so far,
        #: since a damaged copy is often truncated at the damage.
        self.ref_bits = first.bits
        self.has_good = False
        self.add(first)

    def add(self, s: Sighting) -> None:
        self.sightings.append(s)
        self.last = max(self.last, s.when)
        if len(s.bits) > len(self.ref_bits):
            self.ref_bits = s.bits
        if s.packet.crc_ok is True:
            self.has_good = True
            self.key = message_key(s.packet)

    def is_closed(self, now: float) -> bool:
        deadline = min(
            self.last + self.cfg.window_s + self.cfg.extend_s * (len(self.sightings) - 1),
            self.opened + self.cfg.max_window_s,
        )
        return now > deadline

    def build(self, allow_combine: bool) -> FusedEvent:
        views: dict[str, ReceiverView] = {}
        for s in self.sightings:
            v = views.setdefault(s.receiver, ReceiverView())
            v.copies += 1
            if s.rssi_dbm is not None and (v.rssi_dbm is None or s.rssi_dbm > v.rssi_dbm):
                v.rssi_dbm = s.rssi_dbm
            if s.snr_db is not None and (v.snr_db is None or s.snr_db > v.snr_db):
                v.snr_db = s.snr_db
            if s.packet.crc_ok is True:
                v.crc_ok = True
            v.best_hops_left = max(v.best_hops_left, s.packet.hops_left)

        good = [s for s in self.sightings if s.packet.crc_ok is True]
        combined = False
        combined_from = 0

        if good:
            # Prefer the least-travelled copy, then the strongest.
            best = max(
                good,
                key=lambda s: (s.packet.hops_left, s.rssi_dbm if s.rssi_dbm is not None else -999),
            )
            packet = best.packet
        else:
            packet = self.sightings[0].packet
            if allow_combine:
                got = combine(self.sightings)
                if got is not None:
                    packet, combined_from = got
                    combined = True

        hops = [s.packet.hops_left for s in self.sightings]
        # Closest to the source: most hops left wins, then loudest.
        closest = max(
            views.items(),
            key=lambda kv: (kv[1].best_hops_left,
                            kv[1].rssi_dbm if kv[1].rssi_dbm is not None else -999),
        )[0] if views else None

        return FusedEvent(
            key=self.key,
            packet=packet,
            first_seen=self.opened,
            last_seen=self.last,
            hops_left_min=min(hops),
            hops_left_max=max(hops),
            receivers=views,
            closest=closest,
            combined=combined,
            combined_from=combined_from,
        )


def fuse_all(sightings: Iterable[Sighting], **kwargs: Any) -> Iterator[FusedEvent]:
    """Run a finite sequence of sightings through :class:`Fusion` (for tests
    and offline replay of a JSON-lines capture)."""
    f = Fusion(**kwargs)
    last = 0.0
    for s in sorted(sightings, key=lambda s: s.when):
        last = s.when
        yield from f.pop_ready(s.when)
        f.add(s)
    yield from f.flush(last + f.max_window_s + 1.0)


__all__ = [
    "EXTEND_S",
    "MAX_COMBINE_FLIPS",
    "MAX_DISAGREEMENTS",
    "MAX_WINDOW_S",
    "REPEAT_MEMORY_S",
    "WINDOW_S",
    "FusedEvent",
    "Fusion",
    "ReceiverView",
    "Sighting",
    "Vote",
    "combine",
    "hamming",
    "sightings_from_capture",
    "fuse_all",
]
