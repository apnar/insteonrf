"""Fuse sightings of one Insteon message from several receivers.

The mesh (``Doc/MESH-PLAN.md``) has every board report independently to MQTT
and does all the combining here. That choice removes any need for clock
synchronisation between boards, because the thing we actually want to know —
which copy is closest to the transmitter — is carried *in the packet*: the
flags byte holds hops-left, and the copy with the most hops left has travelled
least. Timestamps are used only to decide which sightings belong together.

That makes them load-bearing all the same. Two clocks are in play, and they
must not be mixed up:

* **On-air time** (:attr:`Sighting.when`) -- when the receiver says the packet
  was on the air. Every receiver stamps the *start* of its capture, and
  :class:`ClockSkew` learns and removes whatever constant offset each one
  still has. Grouping and windows are in this clock.
* **Arrival time** (the ``now`` passed to :meth:`Fusion.pop_ready`) -- when
  the capture reached this process. Receivers deliver late by different
  amounts (the dongle a block's length, the SDR a demodulation pass, a board
  a WiFi hop), so a bucket is only closed ``lateness_s`` after its on-air
  deadline. A copy that turns up later still is a *straggler*: counted per
  receiver in :attr:`Fusion.late` and dropped, never re-emitted as a
  retransmission.

Measured 2026-09-23 before these existed: the Heltec stamped whole seconds,
the dongle stamped the end of its 224 ms block and the SDR the end of a
demodulation backlog, and one transmission heard by all three receivers came
out as up to three events -- each receiver looking as if it had heard what the
others missed. Of 3,858 events in a day, 367 were credited to all three.

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
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import combinations
from statistics import median
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
#: How long past a bucket's on-air deadline to wait, in arrival time, for
#: receivers that deliver late. It has to cover the slowest receiver's
#: capture-to-arrival delay; the mesh reports those per receiver
#: (``Fusion.arrival_lag``) so this can be checked rather than guessed.
LATENESS_S = 1.0

#: On-air bit rate, for placing a packet within its capture in time.
BAUD = 9124
#: Consecutive hop repeats start this far apart: 456 bits, six half-cycles of
#: 60 Hz (measured 2026-09-15, see ``Doc/MESH-TRANSMIT.md``).
SLOT_S = 456 / BAUD

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
    #: The receiver's own timestamp, before :class:`ClockSkew` corrected it.
    raw_when: float | None = None

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
        # A capture's timestamp is its first bit; a packet further in went
        # out that much later. One capture routinely holds a message and its
        # next hop repeat 50 ms on, and stamping both with the capture's time
        # would make the second look like it came from a receiver with a
        # different clock.
        when = None if timestamp is None else timestamp + pos / BAUD
        pkt = Packet(data, bits[pos:end], when,
                     complete=bool(idx) and idx[-1] == 0)
        pkt.index_ok = indexes_ok(data, idx)
        pkt.rssi_dbm = rssi_dbm
        out.append(Sighting(pkt, receiver, rssi_dbm, when, bits[pos:],
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
        if pkt.crc_ok is True and pkt.index_ok is not False and pkt.hops_ok:
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


# --------------------------------------------------------------------------- clocks


class ClockSkew:
    """Learn each receiver's constant clock offset from messages they share.

    Every receiver is meant to stamp the on-air time of its capture's first
    bit, but each gets there differently -- SNTP on a board, the host clock
    minus a block length for the dongle, a sample counter for the SDR -- and
    each can be off by a constant. A shared message is a reference: when
    several receivers verified the same transmission, where each one placed
    it, less the consensus, is a sample of that receiver's offset.

    Offsets are relative to the consensus of the receivers themselves; there
    is no reference clock to be right against, and grouping needs none.
    Hop copies are normalised to the originating slot first
    (``SLOT_S`` per hop used), so a receiver that only heard the last hop
    is not mistaken for a slow clock.

    A median over the last ``keep`` samples, applied only once ``min_samples``
    have been seen and clamped to ``max_abs_s``: a wrong offset would stop the
    very matching it is learned from, so it has to be slow and hard to fool.
    """

    def __init__(self, *, min_samples: int = 20, keep: int = 200, max_abs_s: float = 2.0):
        self.min_samples = min_samples
        self.keep = keep
        self.max_abs_s = max_abs_s
        self._samples: dict[str, deque[float]] = {}
        self._offset: dict[str, float] = {}

    def offset(self, receiver: str) -> float:
        return self._offset.get(receiver, 0.0)

    def correct(self, receiver: str, when: float) -> float:
        return when - self.offset(receiver)

    def learn(self, raw: dict[str, float]) -> None:
        """One shared message: ``raw`` is each receiver's uncorrected origin time."""
        if len(raw) < 2:
            return
        consensus = median(t - self.offset(r) for r, t in raw.items())
        for r, t in raw.items():
            q = self._samples.setdefault(r, deque(maxlen=self.keep))
            q.append(t - consensus)
        ready = {r: median(q) for r, q in self._samples.items() if len(q) >= self.min_samples}
        if not ready:
            return
        # Relative offsets only: re-centre so the receivers' median is zero,
        # which keeps the set from drifting as a whole.
        centre = median(ready.values())
        for r, v in ready.items():
            self._offset[r] = max(-self.max_abs_s, min(self.max_abs_s, v - centre))

    def to_dict(self) -> dict[str, Any]:
        return {r: {"offset_ms": round(self.offset(r) * 1000.0, 1), "samples": len(q)}
                for r, q in sorted(self._samples.items())}


# --------------------------------------------------------------------------- fusion


class Fusion:
    """Collect sightings into :class:`FusedEvent` objects.

    Buckets stay open for ``window_s`` after the last sighting, extended by
    ``extend_s`` each time and capped at ``max_window_s`` -- all in on-air
    time -- and are then held ``lateness_s`` more, in arrival time, for
    receivers that deliver late. :meth:`pop_ready` hands over the ones that
    have closed.

    ``lateness_s`` defaults to zero here, where ``now`` and the sightings'
    times are the same clock (tests, and :func:`fuse_all` replaying a file);
    the mesh service, which is fed live, uses :data:`LATENESS_S`.
    """

    def __init__(
        self,
        window_s: float = WINDOW_S,
        *,
        extend_s: float = EXTEND_S,
        max_window_s: float = MAX_WINDOW_S,
        repeat_memory_s: float = REPEAT_MEMORY_S,
        allow_combine: bool = True,
        lateness_s: float = 0.0,
        skew: ClockSkew | None = None,
    ):
        self.window_s = window_s
        self.extend_s = extend_s
        self.max_window_s = max_window_s
        self.repeat_memory_s = repeat_memory_s
        self.allow_combine = allow_combine
        self.lateness_s = lateness_s
        #: Per-receiver clock correction; ``None`` to take timestamps as given.
        self.skew = skew
        self._open: dict[tuple[Any, ...], _Bucket] = {}
        #: Buckets a newer transmission of the same key has already ended.
        self._ready: list[_Bucket] = []
        #: key -> (closed at, repeat index, on-air first seen, on-air last seen)
        self._closed: dict[tuple[Any, ...], tuple[float, int, float, float]] = {}
        #: Per receiver: copies that arrived after their message's event had
        #: been emitted. Non-zero means ``lateness_s`` is too short for it.
        self.late: dict[str, int] = {}
        #: Repaired copies merged into their message's event rather than
        #: emitted as one of their own.
        self.folded = 0
        #: Per receiver: recent (arrival - on-air) delays, seconds.
        self._lag: dict[str, deque[float]] = {}

    # -- input ------------------------------------------------------------
    def add(self, sighting: Sighting, *, arrived: float | None = None) -> None:
        """Fold one sighting into the open buckets.

        ``arrived`` is when it reached this process, for the per-receiver
        delay statistics; leave it out when replaying.
        """
        if self.skew is not None:
            raw = sighting.when
            sighting.raw_when = raw
            sighting.timestamp = self.skew.correct(sighting.receiver, raw)
        if arrived is not None:
            self._lag.setdefault(sighting.receiver, deque(maxlen=500)).append(
                arrived - sighting.when)
        key = message_key(sighting.packet)
        bucket = self._open.get(key)
        if bucket is not None:
            if bucket.accepts(sighting.when):
                bucket.add(sighting)
                return
            # Same bytes, but past that bucket's window: a retransmission, not
            # a copy. End the old one now rather than let the key carry both.
            del self._open[key]
            self._ready.append(bucket)
        elif self._is_straggler(key, sighting):
            self.late[sighting.receiver] = self.late.get(sighting.receiver, 0) + 1
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

    def _is_straggler(self, key: tuple[Any, ...], sighting: Sighting) -> bool:
        """A copy of a message whose event has already been emitted."""
        if sighting.packet.crc_ok is not True:
            return False
        seen = self._closed.get(key)
        if seen is None:
            return False
        _, _, first, last = seen
        return first - self.window_s <= sighting.when <= last + self.window_s

    def _nearest(self, sighting: Sighting) -> _Bucket | None:
        """The open bucket whose bits are closest, if close enough to be the
        same transmission."""
        incoming_good = sighting.packet.crc_ok is True
        best: _Bucket | None = None
        best_d = MAX_DISAGREEMENTS + 1
        for bucket in self._open.values():
            if incoming_good and bucket.has_good:
                continue  # two confirmed packets: trust their bytes
            if not bucket.accepts(sighting.when):
                continue
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
        done, self._ready = self._ready, []
        for key, bucket in list(self._open.items()):
            if bucket.is_closed(now - self.lateness_s):
                del self._open[key]
                done.append(bucket)
        return self._emit(done, now)

    def _emit(self, done: list[_Bucket], now: float) -> list[FusedEvent]:
        """Build events from closed buckets, folding repaired copies home.

        A damaged copy cannot join its message's bucket on arrival: its key
        is wrong, and a *different hop's* copy is too far away in bits to
        match by similarity (the hop field and the CRC differ, 16 on-air
        symbols before any damage). So it gets a bucket of its own, and once
        :func:`combine` has repaired it, its real key is that message's. Left
        alone, that came out as a second event -- numbered as a
        retransmission, heard by one receiver -- which is exactly the
        inconsistency the mesh is meant to measure. Seen live 2026-09-23 on
        the V4, whose soft copies are the ones most often repaired alone.

        So buckets with no verified copy are built first, and a repaired one
        is merged into a bucket for the same message that is still pending
        or open, or, if that event has already gone, counted as late.
        """
        done = sorted(done, key=lambda b: b.opened)
        pending = [b for b in done if b.has_good]
        out: list[FusedEvent] = []
        for b in done:
            if b.has_good:
                continue
            event = b.build(self.allow_combine)
            if event.combined:
                key = message_key(event.packet)
                home = next((g for g in pending if g.key == key and g.accepts(b.opened)), None)
                if home is None:
                    live = self._open.get(key)
                    if live is not None and live.accepts(b.opened):
                        home = live
                if home is not None:
                    for s in b.sightings:
                        home.add(s)
                    self.folded += 1
                    continue
                seen = self._closed.get(key)
                if seen is not None and seen[2] - self.window_s <= b.opened <= seen[3] + self.window_s:
                    for s in b.sightings:
                        self.late[s.receiver] = self.late.get(s.receiver, 0) + 1
                    self.folded += 1
                    continue
            out.append(self._finish(b, now, event))
        out += [self._finish(b, now) for b in pending]
        out.sort(key=lambda e: e.first_seen)
        return out

    def flush(self, now: float | None = None) -> list[FusedEvent]:
        """Close every open bucket (use at shutdown)."""
        if now is None:
            now = time.time()
        done = [*self._ready, *self._open.values()]
        self._ready = []
        self._open.clear()
        return self._emit(done, now)

    def __len__(self) -> int:
        return len(self._open) + len(self._ready)

    def arrival_lag(self) -> dict[str, dict[str, int]]:
        """Per receiver, how late its captures arrive: median, p95 and worst, ms."""
        out = {}
        for r, q in sorted(self._lag.items()):
            v = sorted(q)
            if v:
                out[r] = {"median_ms": round(v[len(v) // 2] * 1000.0),
                          "p95_ms": round(v[min(len(v) - 1, int(len(v) * 0.95))] * 1000.0),
                          "max_ms": round(v[-1] * 1000.0)}
        return out

    # -- internals --------------------------------------------------------
    def _forget_old(self, now: float) -> None:
        for key, (when, *_rest) in list(self._closed.items()):
            if now - when > self.repeat_memory_s:
                del self._closed[key]

    def _finish(self, bucket: _Bucket, now: float,
                event: FusedEvent | None = None) -> FusedEvent:
        if event is None:
            event = bucket.build(self.allow_combine)
        # A bucket opened by a damaged copy carries that copy's (wrong) key.
        # Once combining has recovered the real bytes, re-key the event so
        # repeat numbering counts the actual message.
        if event.combined:
            event.key = message_key(event.packet)
        seen = self._closed.get(event.key)
        event.repeat_index = 0 if seen is None else seen[1] + 1
        self._closed[event.key] = (now, event.repeat_index, event.first_seen, event.last_seen)
        if self.skew is not None and not event.combined:
            self.skew.learn(bucket.origins())
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

    def accepts(self, when: float) -> bool:
        """Whether a copy on the air at ``when`` can belong to this bucket."""
        return self.opened - self.cfg.window_s <= when <= self.deadline()

    def origins(self) -> dict[str, float]:
        """Per receiver with a verified copy, its uncorrected estimate of when
        the message first went out: each copy's time less the hops it had
        already used, earliest per receiver."""
        out: dict[str, float] = {}
        for s in self.sightings:
            p = s.packet
            if p.crc_ok is not True or not p.hops_ok:
                continue
            t = (s.raw_when if s.raw_when is not None else s.when) - \
                (p.max_hops - p.hops_left) * SLOT_S
            if s.receiver not in out or t < out[s.receiver]:
                out[s.receiver] = t
        return out

    def add(self, s: Sighting) -> None:
        self.sightings.append(s)
        self.opened = min(self.opened, s.when)
        self.last = max(self.last, s.when)
        if len(s.bits) > len(self.ref_bits):
            self.ref_bits = s.bits
        if s.packet.crc_ok is True:
            self.has_good = True
            self.key = message_key(s.packet)

    def deadline(self) -> float:
        """The latest on-air time a copy can have and still belong here."""
        return min(
            self.last + self.cfg.window_s + self.cfg.extend_s * (len(self.sightings) - 1),
            self.opened + self.cfg.max_window_s,
        )

    def is_closed(self, now: float) -> bool:
        return now > self.deadline()

    def build(self, allow_combine: bool) -> FusedEvent:
        views: dict[str, ReceiverView] = {}
        for s in self.sightings:
            v = views.setdefault(s.receiver, ReceiverView())
            v.copies += 1
            if s.rssi_dbm is not None and (v.rssi_dbm is None or s.rssi_dbm > v.rssi_dbm):
                v.rssi_dbm = s.rssi_dbm
            if s.snr_db is not None and (v.snr_db is None or s.snr_db > v.snr_db):
                v.snr_db = s.snr_db
            if s.packet.crc_ok is True and s.packet.hops_ok:
                v.crc_ok = True
            v.best_hops_left = max(v.best_hops_left, s.packet.hops_left)

        # A verified copy has to survive the flags byte too. hops-left above
        # max-hops is a flags byte no transmitter emits, and it is the only
        # thing that distinguishes a false header lock inside the tail of a
        # real capture -- whose frame counters and CRC can both pass by luck
        # -- from a message. See Packet.hops_ok.
        good = [s for s in self.sightings if s.packet.crc_ok is True and s.packet.hops_ok]
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
    "BAUD",
    "EXTEND_S",
    "LATENESS_S",
    "SLOT_S",
    "ClockSkew",
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
