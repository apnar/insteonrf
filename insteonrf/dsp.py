"""Software modulator/demodulator for the Insteon 915 MHz 2-FSK link.

This is the SDR half of the toolkit: :func:`modulate_fsk2` turns an on-air bit
string (from :meth:`insteonrf.packet.Packet.to_bits`) into baseband I/Q samples
that ``hackrf_transfer -t`` can transmit, and :func:`demodulate_fsk2` does the
reverse for a recorded or piped capture — the same job as the C ``fsk2_demod``
binary, in numpy, so no compiler is needed.

Sample format matches the SDR tools and the C demodulator:

* ``signed=True``  — interleaved ``int8`` I/Q, what HackRF uses (``fsk2_demod -U``).
* ``signed=False`` — interleaved ``uint8`` I/Q offset by 128, rtl-sdr style
  (``fsk2_demod -u``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np

DEFAULT_SAMPLE_RATE = 2_400_000
DEFAULT_BAUD = 9124
DEFAULT_DEVIATION = 75_000
DEFAULT_AMPLITUDE = 100

#: On-air preamble pattern. Insteon sends a Manchester-coded ``0101…`` which,
#: inverted for transmission, is this 4-bit cell repeated (the ``0x6666`` sync
#: word the rfcat dongle uses). The sequence is its own inverse shifted by two
#: bits, so it splices onto a bit string of either polarity.
PREAMBLE_CELL = "0110"

#: Preamble cell in *logical* (un-inverted) polarity, as ``to_bits(invert=False)``
#: emits it: the Manchester coding of ``0101…``.
PREAMBLE_CELL_LOGICAL = "1001"

#: How many preamble cells of context the sync correlator uses. Real devices
#: send roughly five, so four keeps a margin.
SYNC_PREAMBLE_CELLS = 4

#: The shortest possible packet, in on-air symbols: 13 frames of 28 bits. Two
#: packets cannot begin closer together than this, which bounds sync spacing.
MIN_PACKET_SYMBOLS = 13 * 28

#: Where ``START_HEADER`` begins within the sync template. The header's first
#: five bits *are* the preamble's tail (the cell has period 4), so the header
#: starts before the template's preamble section ends.
HEADER_SYMBOL_IN_TEMPLATE = SYNC_PREAMBLE_CELLS * 4 - 5


def _bit_array(bits: str) -> np.ndarray[Any, Any]:
    a = np.frombuffer(bits.encode("ascii"), dtype=np.uint8)
    bad = (a != ord("0")) & (a != ord("1"))
    if bad.any():
        i = int(np.argmax(bad))
        raise ValueError(f"invalid bit {bits[i]!r} at position {i}")
    return cast("np.ndarray[Any, Any]", (a == ord("1")).astype(np.int8))


def _tone_bits(cell: str, duration_s: float, baud: float) -> str:
    n = max(0, round(duration_s * baud))
    if not n:
        return ""
    return (cell * (n // len(cell) + 1))[:n]


def modulate_fsk2(
    bits: str,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    baud: float = DEFAULT_BAUD,
    deviation: float = DEFAULT_DEVIATION,
    signed: bool = True,
    amplitude: int = DEFAULT_AMPLITUDE,
    preamble_s: float = 0.002,
    trailer_s: float = 0.001,
    ramp_s: float = 50e-6,
) -> np.ndarray[Any, Any]:
    """Modulate an on-air bit string into interleaved 8-bit I/Q samples.

    Bit ``1`` is sent as ``+deviation`` and bit ``0`` as ``-deviation`` — the
    CC1111 convention, and the polarity real captures show (demodulating
    ``Dat/41802513110D2711018C00.dat`` with a standard discriminator yields the
    inverted start header that the rfcat dongle syncs on). Both demodulators
    here decode these samples back to identical bytes with no ``-i``.

    A preamble tone (``PREAMBLE_CELL`` at the bit rate) is prepended and
    appended so the demodulator's squelch and bit-timing recovery have settled
    before the start header arrives; the envelope is ramped over ``ramp_s`` at
    each end to limit splatter. Bit timing uses a fractional phase accumulator
    — at 2.4 Msps and 9124 baud a bit is 263.05 samples, and rounding that to
    263 would slip a whole bit over a 480-bit extended packet.
    """
    if not 1 <= amplitude <= 127:
        raise ValueError("amplitude must be 1..127")
    if baud <= 0 or sample_rate <= 0:
        raise ValueError("sample_rate and baud must be positive")
    if sample_rate < 4 * baud:
        raise ValueError("sample_rate must be at least 4x the baud rate")

    full = _tone_bits(PREAMBLE_CELL, preamble_s, baud) + bits
    full += _tone_bits(PREAMBLE_CELL, trailer_s, baud)
    sym = _bit_array(full)
    if sym.size == 0:
        return np.zeros(0, dtype=np.uint8 if not signed else np.int8)

    sps = sample_rate / baud
    n = int(round(sym.size * sps))
    # Fractional bit clock: sample n belongs to bit floor(n / sps).
    idx = np.minimum((np.arange(n) / sps).astype(np.int64), sym.size - 1)
    freq = np.where(sym[idx] == 1, deviation, -deviation)
    phase = np.cumsum(2.0 * np.pi * freq / sample_rate)

    env = np.ones(n)
    r = min(int(round(ramp_s * sample_rate)), n // 2)
    if r > 0:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)  # raised cosine
        env[:r] = ramp
        env[n - r :] = ramp[::-1]

    iq = np.empty(2 * n, dtype=np.float64)
    iq[0::2] = amplitude * env * np.cos(phase)
    iq[1::2] = amplitude * env * np.sin(phase)
    rounded = np.rint(iq)
    if signed:
        return cast("np.ndarray[Any, Any]", np.clip(rounded, -127, 127).astype(np.int8))
    return cast("np.ndarray[Any, Any]", np.clip(rounded + 128, 0, 255).astype(np.uint8))


# --------------------------------------------------------------------------- demodulation


def _to_complex(samples: np.ndarray[Any, Any] | bytes, *, signed: bool) -> np.ndarray[Any, Any]:
    """Interleaved 8-bit I/Q (bytes or array) to a complex baseband array."""
    if isinstance(samples, (bytes, bytearray, memoryview)):
        samples = np.frombuffer(samples, dtype=np.int8 if signed else np.uint8)
    a = np.asarray(samples)
    if a.dtype == np.uint8:
        a = a.astype(np.float32) - 128.0
    elif a.dtype == np.int8:
        a = a.astype(np.float32)
    else:
        a = a.astype(np.float32, copy=False)
    if a.size % 2:
        a = a[:-1]
    return cast("np.ndarray[Any, Any]", a[0::2] + 1j * a[1::2])


@dataclass
class Burst:
    """One demodulated burst, with per-symbol confidence.

    ``soft[i]`` is a signed confidence for on-air symbol ``i``: its sign is the
    hard decision (positive = ``1``) and its magnitude is how sure the detector
    is, normalised so 1.0 is a clean symbol. :mod:`insteonrf.recover` uses it to
    combine Manchester pairs and to rank candidates for error correction, which
    is worth a few dB over hard-deciding each symbol on its own.
    """

    bits: str
    soft: np.ndarray[Any, Any] = field(repr=False)
    start: int = 0
    sps: float = 0.0
    cfo_hz: float = 0.0
    snr_db: float = 0.0
    #: Which polarity of the sync template matched: ``-1`` for a normal
    #: on-air packet (the wire is the inverse of the logical bit string),
    #: ``+1`` for one modulated from ``to_bits(invert=False)``. ``bits`` is
    #: always as transmitted, whichever matched.
    polarity: int = 1
    #: Index into ``bits`` where ``START_HEADER`` begins, when the sync
    #: correlator located it — so the frame grid is known rather than searched.
    header_index: int | None = None
    timestamp: float | None = None

    def __len__(self) -> int:
        return len(self.bits)


def _sync_template() -> str:
    """The known bit pattern at the head of every packet, logical polarity.

    ``SYNC_PREAMBLE_CELLS`` cells of Manchester-coded preamble followed by the
    rest of :data:`insteonrf.packet.START_HEADER` (the frame marker plus the
    Manchester coding of frame index 31). The preamble cell has period 4 and
    the header's first five bits are its tail, so the two splice cleanly.
    """
    from .packet import MARKER_OFFSET, START_HEADER

    return PREAMBLE_CELL_LOGICAL * SYNC_PREAMBLE_CELLS + START_HEADER[MARKER_OFFSET:]


def _resample_template(bits: str, sps: float) -> np.ndarray[Any, Any]:
    """A ±1 frequency template for ``bits`` at ``sps`` samples per symbol."""
    sym = _bit_array(bits).astype(np.float32) * 2.0 - 1.0
    n = int(round(len(bits) * sps))
    idx = np.minimum((np.arange(n) / sps).astype(np.int64), sym.size - 1)
    return cast("np.ndarray[Any, Any]", sym[idx])


def find_sync(
    disc: np.ndarray[Any, Any],
    sps: float,
    *,
    decim: int = 8,
    threshold: float = 0.55,
    max_syncs: int = 4,
    min_gap_symbols: int = MIN_PACKET_SYMBOLS,
    rel_threshold: float = 0.25,
) -> list[tuple[int, float, int]]:
    """Locate packet starts by correlating against the known head pattern.

    Returns ``(sample offset of the template start, score, polarity)`` per
    detection, best first, at most ``max_syncs``. ``disc`` is discriminator
    output (instantaneous frequency); a carrier frequency offset is just a DC
    term there, so correlating mean-removed signal against a mean-removed
    template is immune to it.

    Manchester-coded payload contains plenty of preamble-like runs, so
    candidates are filtered three ways: an absolute score floor, a minimum
    spacing of one whole packet (two packets cannot start closer together than
    that), and a *relative* floor — anything more than ``rel_threshold`` below
    the best score in this burst is payload, not a packet. Measured on real
    and synthetic captures, a true sync scores 0.9-1.0 while the best payload
    false alarm sits near 0.7.

    This replaces "first zero crossing after the squelch opened" as the timing
    reference: it locks to the frame, so symbol numbering is right even when
    the burst starts mid-preamble or the squelch opened late.
    """
    template = _resample_template(_sync_template(), sps)
    if disc.size < template.size:
        return []
    t = template[::decim].astype(np.float32)
    t = t - t.mean()
    norm = float(np.sqrt((t * t).sum()))
    if norm == 0:
        return []
    d = disc[: (disc.size // decim) * decim].reshape(-1, decim).mean(axis=1).astype(np.float32)
    if d.size < t.size:
        return []
    # Normalised cross-correlation, mean-removed over each window.
    corr = np.correlate(d, t, mode="valid")
    win = t.size
    csum = np.concatenate(([0.0], np.cumsum(d, dtype=np.float64)))
    csum2 = np.concatenate(([0.0], np.cumsum(d.astype(np.float64) ** 2)))
    k = np.arange(corr.size)
    wsum = csum[k + win] - csum[k]
    wsum2 = csum2[k + win] - csum2[k]
    var = np.maximum(wsum2 - wsum * wsum / win, 1e-9)
    score = corr / (norm * np.sqrt(var))

    guard = max(1, int(round(min_gap_symbols * sps / decim)))
    peaks = np.flatnonzero(np.abs(score) > threshold)
    if peaks.size == 0:
        return []
    order = peaks[np.argsort(-np.abs(score[peaks]))]
    floor = float(abs(score[order[0]])) - rel_threshold
    taken: list[int] = []
    out: list[tuple[int, float, int]] = []
    for idx in order:
        i = int(idx)
        if abs(score[i]) < floor:
            break  # sorted by score: everything after this is payload too
        if any(abs(i - t) <= guard for t in taken):
            continue
        taken.append(i)
        out.append((i * decim, float(abs(score[i])), 1 if score[i] > 0 else -1))
        if len(out) >= max_syncs:
            break
    return out


def _ml_soft(
    x: np.ndarray[Any, Any],
    start: float,
    sps: float,
    deviation: float,
    sample_rate: float,
    *,
    cfo_hz: float = 0.0,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Noncoherent matched-filter detection: correlate each symbol with both tones.

    Returns ``(soft, matched, unmatched)``: ``soft`` is ``|S+| - |S-|``
    normalised to ±1, ``matched`` is the larger magnitude (signal plus noise)
    and ``unmatched`` the smaller (noise only), which together give an SNR
    estimate.

    This is the optimal detector for noncoherent 2-FSK and buys ~2-3 dB over a
    discriminator followed by integrate-and-dump, because it matches the actual
    signal shape instead of differentiating phase (which amplifies noise).
    """
    n = np.arange(x.size, dtype=np.float64)
    if cfo_hz:
        x = x * np.exp(-2j * np.pi * cfo_hz * n / sample_rate)
    rot = 2 * np.pi * deviation * n / sample_rate
    yp = np.cumsum(x * np.exp(-1j * rot))
    ym = np.cumsum(x * np.exp(+1j * rot))
    yp = np.concatenate(([0], yp))
    ym = np.concatenate(([0], ym))

    nsym = max(0, int((x.size - start) / sps))
    if nsym == 0:
        empty = np.zeros(0, dtype=np.float32)
        return empty, empty, empty
    k = np.arange(nsym)
    lo = np.rint(start + k * sps).astype(np.int64)
    hi = np.rint(start + (k + 1) * sps).astype(np.int64)
    lo = np.clip(lo, 0, x.size)
    hi = np.clip(hi, 0, x.size)
    width = np.maximum(hi - lo, 1)
    sp = np.abs(yp[hi] - yp[lo]) / width
    sm = np.abs(ym[hi] - ym[lo]) / width
    scale = np.maximum(np.maximum(sp, sm), 1e-9)
    soft = ((sp - sm) / scale).astype(np.float32)
    matched = np.maximum(sp, sm).astype(np.float32)
    unmatched = np.minimum(sp, sm).astype(np.float32)
    return soft, matched, unmatched


def _rate_candidates(coarse: float = 0.005, step: float = 0.001) -> np.ndarray[Any, Any]:
    """Symbol-rate hypotheses to try, as fractional offsets around nominal.

    Insteon devices run cheap crystals: a few hundred ppm of baud error walks
    the bit clock by a fraction of a symbol over a 1000-symbol extended packet,
    which a fixed nominal rate cannot follow. Trying a small grid and keeping
    the most confident result is more robust here than a tracking loop, because
    a burst is short and we can afford to demodulate it several times.
    """
    n = int(round(coarse / step))
    return np.arange(-n, n + 1) * step


def demodulate_bursts(
    samples: np.ndarray[Any, Any] | bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    baud: float = DEFAULT_BAUD,
    deviation: float = DEFAULT_DEVIATION,
    signed: bool = True,
    squelch: float = 12.0,
    method: str = "ml",
    min_bits: int = 64,
    refine_rate: bool = True,
    sync_threshold: float = 0.45,
    timestamp: float | None = None,
) -> list[Burst]:
    """Demodulate I/Q into :class:`Burst` objects with soft decisions.

    The chain is: envelope squelch to find candidate bursts → correlate the
    known preamble/start-header pattern to lock frame timing, polarity and the
    carrier frequency offset → noncoherent matched-filter detection of every
    symbol, optionally repeated over a small symbol-rate grid keeping the most
    confident result.

    ``method="discriminator"`` selects the older, cheaper chain (phase
    discriminator plus integrate-and-dump) for comparison.
    """
    x = _to_complex(samples, signed=signed)
    if x.size < 8:
        return []
    sps = sample_rate / baud
    win = max(1, int(round(sps)))
    env = cast("np.ndarray[Any, Any]", np.convolve(np.abs(x), np.ones(win) / win, mode="same"))
    hot = env > squelch
    if not hot.any():
        return []

    disc = np.angle(x[1:] * np.conj(x[:-1]))
    out: list[Burst] = []
    for lo, hi in _runs(hot):
        if hi - lo < sps * 4:
            continue
        seg = x[lo:hi]
        dseg = disc[lo : min(hi, disc.size)]
        if method == "discriminator":
            bits = _slice_bits(dseg, sps)
            if len(bits) >= min_bits:
                soft = np.where(np.frombuffer(bits.encode("ascii"), dtype=np.uint8)
                                == ord("1"), 1.0, -1.0).astype(np.float32)
                out.append(Burst(bits=bits, soft=soft, start=lo, sps=sps,
                                 timestamp=timestamp))
            continue
        out += _demod_burst(seg, dseg, lo, sps, deviation, sample_rate,
                            min_bits=min_bits, refine_rate=refine_rate,
                            sync_threshold=sync_threshold, timestamp=timestamp)
    return out


def estimate_cfo(
    disc: np.ndarray[Any, Any],
    start: float,
    sps: float,
    sample_rate: float,
    *,
    cells: int = SYNC_PREAMBLE_CELLS,
) -> float:
    """Carrier frequency offset in Hz, from the preamble the sync matched.

    The preamble cell has two ones and two zeros, so its own contribution to
    the mean instantaneous frequency cancels and whatever is left is the
    offset. Estimating it over the *whole* sync template instead would be
    biased, because the start header is not balanced (14 ones of 27 bits ≈
    2.8 kHz of bias at 75 kHz deviation).

    Devices run cheap crystals — the capture in ``Dat/`` sits about 24 kHz off,
    roughly 26 ppm at 915 MHz. Left uncorrected that offset biases every symbol
    decision toward one tone.
    """
    lo = int(round(start))
    hi = int(round(start + cells * 4 * sps))
    seg = disc[max(0, lo) : min(hi, disc.size)]
    if seg.size == 0:
        return 0.0
    return float(seg.mean() * sample_rate / (2 * np.pi))


def _snr_db(matched: np.ndarray[Any, Any], unmatched: np.ndarray[Any, Any]) -> float:
    """Approximate SNR from the matched and unmatched tone correlator outputs.

    The tone the symbol was *not* sent on collects noise only, so the ratio of
    the two magnitudes is a per-symbol SNR estimate. It is a rough figure —
    good enough to rank links and watch a device degrade over weeks, which is
    what it is used for.
    """
    if matched.size == 0:
        return 0.0
    sig = float(matched.mean())
    noise = float(unmatched.mean())
    if noise <= 0 or sig <= 0:
        return 60.0
    return float(np.clip(20 * np.log10(sig / noise), -20.0, 60.0))


def _demod_burst(
    seg: np.ndarray[Any, Any],
    dseg: np.ndarray[Any, Any],
    offset: int,
    sps: float,
    deviation: float,
    sample_rate: float,
    *,
    min_bits: int,
    refine_rate: bool,
    sync_threshold: float,
    timestamp: float | None,
) -> list[Burst]:
    """Demodulate one squelch-delimited burst, from every sync it contains."""
    syncs: list[tuple[int, float, int]] = find_sync(dseg, sps, threshold=sync_threshold)
    synced = bool(syncs)
    if not syncs:
        # No frame lock: demodulate the whole burst from its start anyway, so a
        # packet whose header was corrupted still produces bits to look at.
        syncs = [(0, 0.0, 1)]

    out: list[Burst] = []
    for start, _score, polarity in syncs:
        cfo_hz = estimate_cfo(dseg, float(start), sps, sample_rate) if synced else 0.0
        rates = _rate_candidates() if refine_rate else np.zeros(1)
        best: tuple[float, np.ndarray[Any, Any], np.ndarray[Any, Any],
                    np.ndarray[Any, Any], float] | None = None
        for delta in rates:
            trial = sps * (1.0 + float(delta))
            soft, matched, unmatched = _ml_soft(seg, float(start), trial, deviation,
                                                sample_rate, cfo_hz=cfo_hz)
            if soft.size == 0:
                continue
            confidence = float(np.abs(soft).mean())
            if best is None or confidence > best[0]:
                best = (confidence, soft, matched, unmatched, trial)
        if best is None:
            continue
        _conf, soft, matched, unmatched, trial_sps = best
        # soft is already in transmitted polarity: its sign is the tone the
        # symbol was sent on, and a data 1 is the higher tone. The sync
        # template is written in logical polarity, so a normal (inverted)
        # on-air packet matches it at polarity -1 — that says which template
        # matched, and must not be used to flip the bits.
        bits = "".join("1" if v > 0 else "0" for v in soft)
        if len(bits) < min_bits:
            continue
        out.append(Burst(
            bits=bits,
            soft=soft,
            start=offset + int(start),
            sps=trial_sps,
            cfo_hz=cfo_hz,
            snr_db=_snr_db(matched, unmatched),
            polarity=polarity,
            header_index=HEADER_SYMBOL_IN_TEMPLATE if synced else None,
            timestamp=timestamp,
        ))
    return out


def demodulate_fsk2(
    samples: np.ndarray[Any, Any] | bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    baud: float = DEFAULT_BAUD,
    signed: bool = True,
    squelch: float = 12.0,
    invert: bool = False,
    min_bits: int = 64,
    method: str = "ml",
) -> list[str]:
    """Demodulate interleaved 8-bit I/Q into one bit string per burst.

    Thin wrapper over :func:`demodulate_bursts` that keeps the pipeline's
    string contract (and therefore throws the soft information away — use
    :func:`demodulate_bursts` with :mod:`insteonrf.recover` to keep it).

    Returns the bursts that yielded at least ``min_bits`` bits.
    """
    bursts = demodulate_bursts(samples, sample_rate=sample_rate, baud=baud, signed=signed,
                               squelch=squelch, method=method, min_bits=min_bits)
    if invert:
        from .manchester import invert_bits

        return [invert_bits(b.bits) for b in bursts]
    return [b.bits for b in bursts]


def _runs(mask: np.ndarray[Any, Any]) -> Iterable[tuple[int, int]]:
    """Yield ``(start, stop)`` index pairs for each run of True in ``mask``."""
    d = np.diff(mask.astype(np.int8))
    starts = [int(i) for i in np.flatnonzero(d == 1) + 1]
    stops = [int(i) for i in np.flatnonzero(d == -1) + 1]
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        stops.append(int(mask.size))
    return zip(starts, stops, strict=False)


def _slice_bits(disc: np.ndarray[Any, Any], sps: float, *, invert: bool = False) -> str:
    """Slice one burst of discriminator output into bits at the bit centres."""
    # Bit clock phase: the first sign change of the (smoothed) discriminator is
    # a bit boundary, so bit centres sit half a bit period after it.
    sm = np.convolve(disc, np.ones(3) / 3, mode="same")
    sign = np.sign(sm)
    nz = np.flatnonzero(sign != 0)
    if nz.size == 0:
        return ""
    sign = sign[nz[0] :]
    changes = np.flatnonzero(np.diff(sign) != 0)
    edge = int(nz[0] + (changes[0] + 1 if changes.size else 0))
    first = edge % sps + sps / 2
    if first >= disc.size:
        return ""
    centres = np.arange(first, disc.size - 1, sps)
    # Integrate the discriminator over the middle 60% of each bit instead of
    # sampling it once (what the C demod does): the decision then averages
    # ~160 samples at 2.4 Msps, which tolerates several times more noise.
    half = max(1, int(round(0.3 * sps)))
    csum = np.concatenate(([0.0], np.cumsum(disc)))
    c = centres.astype(np.int64)
    lo = np.clip(c - half, 0, disc.size)
    hi = np.clip(c + half + 1, 0, disc.size)
    vals = (csum[hi] - csum[lo]) / np.maximum(hi - lo, 1)
    ones = vals > 0 if not invert else vals < 0
    return "".join("1" if b else "0" for b in ones)


def iq_bursts(
    samples: np.ndarray[Any, Any] | bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    baud: float = DEFAULT_BAUD,
    signed: bool = True,
    squelch: float = 12.0,
    pad_s: float = 0.0005,
) -> list[np.ndarray[Any, Any]]:
    """Split an I/Q stream into one array per burst (the job of ``rf_clip``)."""
    x = _to_complex(samples, signed=signed)
    if x.size == 0:
        return []
    win = max(1, int(round(sample_rate / baud)))
    env = np.convolve(np.abs(x), np.ones(win) / win, mode="same")
    hot = env > squelch
    if not hot.any():
        return []
    pad = int(round(pad_s * sample_rate))
    a = np.asarray(samples)
    if isinstance(samples, (bytes, bytearray, memoryview)):
        a = np.frombuffer(samples, dtype=np.int8 if signed else np.uint8)
    out = []
    for start, stop in _runs(hot):
        lo = max(0, 2 * (start - pad))
        hi = min(a.size, 2 * (stop + pad))
        if hi - lo >= 2 * win:
            out.append(a[lo:hi])
    return out
