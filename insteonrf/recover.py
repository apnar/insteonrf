"""Soft-decision framing: decode packets using the redundancy Insteon already has.

:func:`insteonrf.packet.parse_bits` hard-decides every on-air symbol and then
Manchester-decodes, which throws away two useful things:

* **Manchester is a rate-1/2 code.** Each logical bit is sent as ``01`` or
  ``10``, so the optimal decision is the *difference* of the pair's soft values,
  not two independent hard calls. That is worth about 3 dB, and it is free.
* **The frame index is known.** Every 28-bit frame carries a 5-bit counter that
  runs 31, then 11…0 (standard) or 30…0 (extended). Those 65+ bits carry no
  information — they are a checksum the protocol hands us, and they are what
  makes CRC-guided repair safe rather than a coin flip.

On top of that, :func:`recover_packets` will flip the least-confident data bits
looking for a CRC match (a bounded Chase search). Every accepted repair must
also satisfy the frame-index check, which is what keeps false positives down:
an 8-bit CRC alone would accept roughly one in 256 random attempts.

Works with or without soft information — given hard bits only (as the rfcat
dongle produces) it treats illegal Manchester pairs as the low-confidence
positions, which still repairs a useful fraction of marginal packets.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np

from .manchester import invert_bits
from .packet import (
    EXT_CRC_INDEX,
    EXT_LEN,
    FLAG_EXT,
    FRAME_BITS,
    MARKER_OFFSET,
    STD_CRC_INDEX,
    STD_LEN,
    Packet,
    expected_indexes,
    ext_crc,
    find_headers,
    pkt_crc,
)

#: Logical bits per frame: 5 index bits then 8 data bits.
FRAME_INDEX_BITS = 5
FRAME_DATA_BITS = 8
FRAME_LOGICAL_BITS = FRAME_INDEX_BITS + FRAME_DATA_BITS

#: Defaults for the repair search. Small on purpose: every extra trial is
#: another chance for the 8-bit CRC to accept garbage.
MAX_FLIPS = 2
CANDIDATES = 10
#: A bit is only a repair candidate if its confidence is this fraction of the
#: packet's median or less — so a clean packet is never "repaired" into a
#: different valid one.
SUSPECT_RATIO = 0.5


@dataclass
class Recovered:
    """A decoded packet plus what it took to get there."""

    packet: Packet
    corrected: int = 0
    index_errors: int = 0
    manchester_errors: int = 0
    min_confidence: float = 1.0

    def __repr__(self) -> str:
        return (f"Recovered({self.packet.summary()!r}, corrected={self.corrected}, "
                f"index_errors={self.index_errors})")


def _soft_for(bits: str, soft: np.ndarray[Any, Any] | None) -> np.ndarray[Any, Any]:
    """Per-symbol signed confidence; ±1 when only hard bits are available."""
    if soft is not None and len(soft) >= len(bits):
        return np.asarray(soft[: len(bits)], dtype=np.float32)
    a = np.frombuffer(bits.encode("ascii"), dtype=np.uint8)
    return np.where(a == ord("1"), 1.0, -1.0).astype(np.float32)


def _logical(sym: np.ndarray[Any, Any], start: int, count: int
             ) -> tuple[list[int], list[float], int]:
    """Soft-combine Manchester pairs into logical bits.

    Returns ``(values, confidences, illegal_pairs)``. A logical 1 is sent as
    ``01``, so its likelihood is ``s[1] - s[0]`` — one subtraction that keeps
    the coding gain hard decisions would discard. ``illegal_pairs`` counts
    pairs whose hard decisions were ``00`` or ``11``, which marks a symbol the
    detector got wrong even when no soft information is available.
    """
    values: list[int] = []
    conf: list[float] = []
    illegal = 0
    for i in range(count):
        a = start + 2 * i
        if a + 1 >= sym.size:
            break
        s0, s1 = float(sym[a]), float(sym[a + 1])
        llr = s1 - s0
        values.append(1 if llr > 0 else 0)
        conf.append(abs(llr))
        if (s0 > 0) == (s1 > 0):
            illegal += 1
    return values, conf, illegal


def _bytes_from(values: list[int], nframes: int) -> list[int]:
    """Assemble the wire bytes from logical bits (data bits are LSB first)."""
    out = []
    for f in range(nframes):
        base = f * FRAME_LOGICAL_BITS + FRAME_INDEX_BITS
        byte = 0
        for b in range(FRAME_DATA_BITS):
            byte |= values[base + b] << b
        out.append(byte)
    return out


def _index_errors(values: list[int], nframes: int, extended: bool) -> int:
    """How many frames carry the wrong index counter."""
    want = expected_indexes(extended)
    bad = 0
    for f in range(min(nframes, len(want))):
        base = f * FRAME_LOGICAL_BITS
        got = 0
        for b in range(FRAME_INDEX_BITS):
            got |= values[base + b] << b
        if got != want[f]:
            bad += 1
    return bad


def _crc_ok(data: list[int], extended: bool) -> bool:
    if extended:
        if len(data) <= EXT_CRC_INDEX:
            return False
        return data[EXT_CRC_INDEX] == pkt_crc(data) and data[22] == ext_crc(data)
    if len(data) <= STD_CRC_INDEX:
        return False
    return data[STD_CRC_INDEX] == pkt_crc(data)


def _repair(values: list[int], conf: list[float], nframes: int, extended: bool,
            *, max_flips: int, candidates: int) -> tuple[list[int], int] | None:
    """Flip the least-confident data bits until both CRC and index checks pass."""
    data_positions = [f * FRAME_LOGICAL_BITS + FRAME_INDEX_BITS + b
                      for f in range(nframes) for b in range(FRAME_DATA_BITS)
                      if f * FRAME_LOGICAL_BITS + FRAME_INDEX_BITS + b < len(values)]
    if not data_positions:
        return None
    median = float(np.median([conf[p] for p in data_positions])) or 1.0
    suspects = [p for p in data_positions if conf[p] <= SUSPECT_RATIO * median]
    suspects.sort(key=lambda p: conf[p])
    suspects = suspects[:candidates]
    if not suspects:
        return None
    trial = list(values)
    for k in range(1, max_flips + 1):
        for combo in combinations(suspects, k):
            for p in combo:
                trial[p] ^= 1
            data = _bytes_from(trial, nframes)
            if _crc_ok(data, extended) and _index_errors(trial, nframes, extended) == 0:
                return data, k
            for p in combo:
                trial[p] ^= 1
    return None


def recover_packets(
    bits: str,
    soft: np.ndarray[Any, Any] | None = None,
    timestamp: float | None = None,
    *,
    header_index: int | None = None,
    max_flips: int = MAX_FLIPS,
    candidates: int = CANDIDATES,
    max_index_errors: int = 1,
    repair: bool = True,
) -> list[Recovered]:
    """Decode every packet in a burst, soft-combining and repairing as needed.

    ``soft`` is the per-symbol confidence from
    :func:`insteonrf.dsp.demodulate_bursts`; without it the decode still gains
    the Manchester legality check. ``header_index`` skips the header search
    when the sync correlator already located the frame grid.
    """
    line = bits.strip()
    sym = _soft_for(line, soft)
    norm, offsets = find_headers(line)
    if norm != line:  # find_headers flipped polarity; the soft values follow
        sym = -sym
    if header_index is not None and header_index not in offsets:
        offsets = [header_index, *offsets]

    out: list[Recovered] = []
    seen: set[tuple[int, ...]] = set()
    for pos in offsets:
        rec = _recover_at(norm, sym, pos, timestamp, max_flips=max_flips,
                          candidates=candidates, max_index_errors=max_index_errors,
                          repair=repair)
        if rec is None:
            continue
        key = tuple(rec.packet.data)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _recover_at(bits: str, sym: np.ndarray[Any, Any], pos: int, timestamp: float | None,
                *, max_flips: int, candidates: int, max_index_errors: int,
                repair: bool) -> Recovered | None:
    """Decode one packet starting at a header offset."""
    first = pos + MARKER_OFFSET
    # Frame 0 gives the flags byte, which says how long the packet is.
    values, conf, illegal = _logical(sym, first + 2, FRAME_LOGICAL_BITS)
    if len(values) < FRAME_LOGICAL_BITS:
        return None
    flags = _bytes_from(values, 1)[0]
    extended = bool(flags & FLAG_EXT)
    nframes = EXT_LEN if extended else STD_LEN

    all_values: list[int] = []
    all_conf: list[float] = []
    illegal = 0
    for f in range(nframes):
        base = first + f * FRAME_BITS + 2
        v, c, bad = _logical(sym, base, FRAME_LOGICAL_BITS)
        if len(v) < FRAME_LOGICAL_BITS:
            nframes = f
            break
        all_values += v
        all_conf += c
        illegal += bad
    if nframes == 0:
        return None

    data = _bytes_from(all_values, nframes)
    idx_bad = _index_errors(all_values, nframes, extended)
    corrected = 0
    if idx_bad > max_index_errors:
        # Whatever the CRC says, a broken frame counter means these bytes are
        # not a packet. This is what rejects CRC luck (1 in 256) on noise.
        return None
    if not _crc_ok(data, extended):
        if not repair:
            return None
        fixed = _repair(all_values, all_conf, nframes, extended,
                        max_flips=max_flips, candidates=candidates)
        if fixed is None:
            return None
        data, corrected = fixed
        idx_bad = 0

    pkt = Packet(data, bits[pos : first + nframes * FRAME_BITS], timestamp, complete=True)
    pkt.corrected = corrected
    pkt.index_ok = True
    if not pkt.hops_ok:
        # Repair is guided by the CRC, so it is the one place a phantom can
        # be *manufactured* rather than merely accepted. hops-left above
        # max-hops is a flags byte no transmitter emits (see Packet.hops_ok),
        # so a repair that produces one repaired the wrong thing.
        return None
    return Recovered(pkt, corrected=corrected, index_errors=idx_bad,
                     manchester_errors=illegal,
                     min_confidence=float(min(all_conf)) if all_conf else 0.0)


def recover_from_burst(burst: Any, **kwargs: Any) -> list[Recovered]:
    """Convenience: recover packets from a :class:`insteonrf.dsp.Burst`."""
    out = recover_packets(burst.bits, burst.soft, burst.timestamp,
                          header_index=burst.header_index, **kwargs)
    for rec in out:
        rec.packet.snr_db = burst.snr_db
    return out


__all__ = [
    "CANDIDATES",
    "MAX_FLIPS",
    "Recovered",
    "expected_indexes",
    "invert_bits",
    "recover_from_burst",
    "recover_packets",
]
