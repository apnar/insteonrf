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

#: The longest packet, in on-air symbols from the start of the sync template:
#: the template's preamble, then an extended packet's 32 frames (index 31,
#: then 30..0), and a little pad. Demodulating further than this from one
#: sync is wasted work -- the next packet in the run has a sync of its own --
#: and it used to be the dominant cost: every rate hypothesis re-demodulated the whole squelch run
#: from the sync to its end, so a busy exchange cost O(packets x run length).
MAX_PACKET_SYMBOLS = HEADER_SYMBOL_IN_TEMPLATE + 32 * 28 + 32


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
    max_syncs: int | None = None,
    min_gap_symbols: int = MIN_PACKET_SYMBOLS,
    rel_threshold: float = 0.25,
    predecimated: bool = False,
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
    if predecimated:
        # Already one value per ``decim`` samples (see _block_disc).
        d = np.asarray(disc, dtype=np.float32)
    else:
        d = disc[: (disc.size // decim) * decim].reshape(-1, decim).mean(axis=1).astype(np.float32)
    if d.size < t.size:
        return []
    # Normalised cross-correlation, mean-removed over each window.
    corr = _xcorr_valid(d, t)
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
        if max_syncs is not None and len(out) >= max_syncs:
            break
    return out


def _xcorr_valid(d: np.ndarray[Any, Any], t: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """``np.correlate(d, t, "valid")``, by FFT once that is cheaper.

    The direct form is O(len(d) x len(t)); a busy half second against the
    ~1250-tap sync template is a few hundred million multiply-adds, which is
    the difference between keeping up with 2.4 Msps and not.
    """
    if d.size * t.size < 2_000_000:
        return np.correlate(d, t, mode="valid")
    n = d.size + t.size - 1
    nfft = 1 << (n - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(d, nfft) * np.conj(np.fft.rfft(t, nfft)), nfft)
    return full[: d.size - t.size + 1].astype(np.float32)


def _tone_sums(
    x: np.ndarray[Any, Any],
    deviation: float,
    sample_rate: float,
    *,
    cfo_hz: float = 0.0,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Running sums of ``x`` mixed down onto each tone, zero-prefixed.

    The integral of one symbol against a tone is then two lookups, whatever
    the symbol boundaries are -- which is what lets :func:`_demod_burst` try a
    grid of symbol rates for the price of index arithmetic instead of
    re-mixing the whole segment for each one.
    """
    n = np.arange(x.size, dtype=np.float64)
    # Both tones in one rotation each, carrier offset folded in.
    yp = np.cumsum(x * np.exp(-2j * np.pi * (cfo_hz + deviation) * n / sample_rate))
    ym = np.cumsum(x * np.exp(-2j * np.pi * (cfo_hz - deviation) * n / sample_rate))
    zero = np.zeros(1, dtype=yp.dtype)
    return np.concatenate((zero, yp)), np.concatenate((zero, ym))


#: Carrier offsets searched by :func:`_template_cfo`. Measured devices sit
#: within about 25 kHz (the capture in ``Dat/`` is 24 kHz off); the block
#: discriminator stays unambiguous to ~70 kHz.
MAX_CFO_HZ = 45_000


#: Carrier-offset hypotheses for :func:`_tone_sync`, Hz. Half-symbol tone
#: windows tolerate a few kHz of residual (the correlation falls off as
#: sinc(f * T/2)), so an 8 kHz grid leaves at most 4 kHz -- a loss of about
#: 1 dB -- across the whole range devices have been seen at.
TONE_SYNC_CFOS = tuple(range(-40_000, 40_001, 8_000))


def _tone_sync(
    seg: np.ndarray[Any, Any],
    sps: float,
    deviation: float,
    sample_rate: float,
    *,
    decim: int,
    threshold: float,
) -> list[tuple[int, float, int]]:
    """Find packet starts where the phase discriminator cannot: weak signals.

    The discriminator is a nonlinearity, and below the FM threshold its
    output is mostly noise -- sync failed from about 15 dB symbol SNR down,
    while the matched filter still decoded to about 9 dB, so weak devices
    were lost to sync alone. Here the frequency track comes from tone
    energies instead, which degrade gracefully: for each carrier-offset
    hypothesis, the difference of the two tones' energies over half a
    symbol, correlated against the known template exactly as
    :func:`find_sync` does. The best hypothesis wins.

    Used only when the discriminator found nothing, so strong signals are
    handled exactly as before and this costs nothing on them.
    """
    n = seg.size // decim
    if n < 4:
        return []
    y = seg[: n * decim].reshape(n, decim).mean(axis=1)
    rate = sample_rate / decim
    win = max(1, int(round(sps / decim / 2)))
    t = np.arange(n, dtype=np.float64) / rate
    best: list[tuple[int, float, int]] = []
    best_score = 0.0
    for cfo in TONE_SYNC_CFOS:
        base = y * np.exp(-2j * np.pi * cfo * t)
        rot = np.exp(-2j * np.pi * deviation * t)
        ep = _window_mag(base * rot, win)
        em = _window_mag(base * np.conj(rot), win)
        track = (ep - em).astype(np.float32)
        # Centre the window on its sample, as the discriminator's is.
        track = np.roll(track, -(win // 2))
        found = find_sync(track, sps, decim=decim, threshold=threshold, predecimated=True)
        if found and found[0][1] > best_score:
            best, best_score = found, found[0][1]
    return best


def _window_mag(z: np.ndarray[Any, Any], win: int) -> np.ndarray[Any, Any]:
    """|sum of z over a trailing window of ``win``|, per sample."""
    c = np.concatenate((np.zeros(1, dtype=z.dtype), np.cumsum(z)))
    out = np.zeros(z.size, dtype=np.float64)
    out[win - 1:] = np.abs(c[win:] - c[:-win])
    return out


def _template_cfo(
    seg: np.ndarray[Any, Any],
    start: int,
    sps: float,
    deviation: float,
    sample_rate: float,
    polarity: int,
) -> float:
    """Carrier offset from the known head of the packet, by FFT.

    The sync template's symbols are known, so the FSK they put on the air is
    known: multiply it out, and what is left over the template is a single
    tone at the carrier offset, found with the processing gain of the whole
    template (~10,000 samples) rather than from the mean of a discriminator.

    That matters because the matched filter needs the offset to within about
    2 kHz: a residual of f over a 110 us symbol rotates the correlation by
    2*pi*f*T, and at 15 kHz that is ten radians -- nothing left. The mean of
    the discriminator, even low-passed, is biased towards zero at the SNR
    where the matched filter still works, so a 20 kHz device went undecoded
    at 13 dB symbol SNR while one at 0 kHz decoded every time.
    """
    template = _sync_template()
    if polarity < 0:
        template = "".join("1" if b == "0" else "0" for b in template)
    n = int(round(len(template) * sps))
    piece = seg[start : start + n]
    if piece.size < n // 2:
        return 0.0
    freq = _resample_template(template, sps)[: piece.size] * deviation
    ref = np.exp(-2j * np.pi * np.cumsum(freq) / sample_rate)
    r = piece * ref
    nfft = 1 << 16
    spec = np.abs(np.fft.fft(r, nfft))
    f = np.fft.fftfreq(nfft, 1.0 / sample_rate)
    band = np.abs(f) <= MAX_CFO_HZ
    k = int(np.argmax(np.where(band, spec, 0.0)))
    # Parabolic interpolation between bins: the bin is 37 Hz, but cheap.
    if 0 < k < nfft - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        den = a - 2 * b + c
        if den != 0:
            return float(f[k] + 0.5 * (a - c) / den * (sample_rate / nfft))
    return float(f[k])


#: Block rate for the sync discriminator: comfortably wider than the signal
#: (±75 kHz deviation plus crystal offset), and at 2.4 Msps a block of 8 --
#: about 9 dB less noise per value than the full rate. The phase step per
#: block stays under pi up to ~70 kHz of offset. See :func:`_sync_decim`.
SYNC_BLOCK_RATE = 300_000


def _sync_decim(sample_rate: float) -> int:
    """Samples per block for the sync discriminator at this sample rate."""
    return max(1, int(sample_rate // SYNC_BLOCK_RATE))


def _block_disc(x: np.ndarray[Any, Any], decim: int) -> np.ndarray[Any, Any]:
    """Instantaneous frequency (radians per *input* sample) at 1/``decim`` rate.

    The I/Q is averaged over blocks first -- a low-pass -- and the phase step
    is taken between block means. Averaging *before* the angle is what makes
    it work on weak signals; averaging after it (what find_sync used to do to
    the full-rate discriminator) averages an already-biased estimate.
    """
    n = x.size // decim
    if n < 2:
        return np.zeros(0, dtype=np.float32)
    xs = x[: n * decim].reshape(n, decim).mean(axis=1)
    return cast("np.ndarray[Any, Any]",
                (np.angle(xs[1:] * np.conj(xs[:-1])) / decim).astype(np.float32))


def _ml_from_sums(
    yp: np.ndarray[Any, Any],
    ym: np.ndarray[Any, Any],
    start: float,
    sps: float,
    nsym: int | None = None,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Per-symbol decisions from :func:`_tone_sums` output; see :func:`_ml_soft`."""
    size = yp.size - 1
    avail = max(0, int((size - start) / sps))
    nsym = avail if nsym is None else min(nsym, avail)
    if nsym <= 0:
        empty = np.zeros(0, dtype=np.float32)
        return empty, empty, empty
    k = np.arange(nsym)
    lo = np.clip(np.rint(start + k * sps).astype(np.int64), 0, size)
    hi = np.clip(np.rint(start + (k + 1) * sps).astype(np.int64), 0, size)
    width = np.maximum(hi - lo, 1)
    sp = np.abs(yp[hi] - yp[lo]) / width
    sm = np.abs(ym[hi] - ym[lo]) / width
    scale = np.maximum(np.maximum(sp, sm), 1e-9)
    soft = ((sp - sm) / scale).astype(np.float32)
    matched = np.maximum(sp, sm).astype(np.float32)
    unmatched = np.minimum(sp, sm).astype(np.float32)
    return soft, matched, unmatched


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
    yp, ym = _tone_sums(x, deviation, sample_rate, cfo_hz=cfo_hz)
    return _ml_from_sums(yp, ym, start, sps)


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
    t0: float | None = None,
) -> list[Burst]:
    """Demodulate I/Q into :class:`Burst` objects with soft decisions.

    ``t0`` is the wall-clock time of the first sample. Given it, each burst
    is stamped with when *its own* first symbol was on the air, rather than
    all of them with ``timestamp`` -- a buffer can hold several messages, and
    fusion groups receivers' sightings by time.

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
    hot = _envelope(x, sps) > squelch
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
    if t0 is not None:
        for b in out:
            b.timestamp = t0 + b.start / sample_rate
    return out


def _envelope(x: np.ndarray[Any, Any], sps: float) -> np.ndarray[Any, Any]:
    """|x| averaged over one symbol, centred -- the squelch's view of power.

    A running sum rather than ``np.convolve``: the direct convolution is 263
    multiply-adds per sample at 2.4 Msps, over half a billion a second, which
    on its own was most of the real-time budget.
    """
    win = max(1, int(round(sps)))
    a = np.abs(x).astype(np.float64)
    c = np.concatenate(([0.0], np.cumsum(a)))
    half = win // 2
    idx = np.arange(a.size)
    lo = np.clip(idx - half, 0, a.size)
    hi = np.clip(idx - half + win, 0, a.size)
    # Same normalisation as mode="same": the edges average over fewer samples
    # but are divided by the full window, exactly as before.
    return cast("np.ndarray[Any, Any]", ((c[hi] - c[lo]) / win).astype(np.float32))


def estimate_cfo(
    disc: np.ndarray[Any, Any],
    start: float,
    sps: float,
    sample_rate: float,
    *,
    cells: int = SYNC_PREAMBLE_CELLS,
    decim: int = 1,
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
    lo = int(round(start / decim))
    hi = int(round((start + cells * 4 * sps) / decim))
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
    # Sync and carrier offset are found on a discriminator taken *after* a
    # low-pass, not on the full-rate one. At 2.4 Msps the per-sample SNR of a
    # weak packet is near 0 dB, below the FM threshold, where the phase
    # difference is dominated by noise and its mean is pulled towards zero --
    # so a carrier offset, which every cheap-crystal device has, tipped the
    # correlator into finding nothing: at 23.7 dB symbol SNR a 9 kHz offset
    # decoded 15%, 20 kHz 5-10%, where 6 kHz decoded 100%.
    decim = _sync_decim(sample_rate)
    dd = _block_disc(seg, decim)
    syncs: list[tuple[int, float, int]] = find_sync(dd, sps, decim=decim,
                                                    threshold=sync_threshold,
                                                    predecimated=True)
    if not syncs and seg.size >= MIN_PACKET_SYMBOLS * sps:
        syncs = _tone_sync(seg, sps, deviation, sample_rate, decim=decim,
                           threshold=sync_threshold)
    synced = bool(syncs)
    if not syncs:
        # No frame lock: demodulate the whole burst from its start anyway, so a
        # packet whose header was corrupted still produces bits to look at.
        syncs = [(0, 0.0, 1)]

    out: list[Burst] = []
    # Enough symbols for the longest packet at the slowest rate tried.
    nsym = MAX_PACKET_SYMBOLS if synced else None
    judge = HEADER_SYMBOL_IN_TEMPLATE + MIN_PACKET_SYMBOLS if synced else None
    # In time order, so each packet's real length is known before the next
    # sync is considered: find_sync can only space syncs by the *shortest*
    # packet, and an extended packet's payload is long enough to hold a
    # convincing false one. Nothing real starts inside a packet.
    busy_until = -1.0
    for start, _score, polarity in sorted(syncs):
        if start < busy_until:
            continue
        cfo_hz = (_template_cfo(seg, int(start), sps, deviation, sample_rate, polarity)
                  if synced else 0.0)
        # Sync is only placed to within a block (``decim`` samples), and the
        # tone sync to within about two; near threshold that is worth half a
        # dB. So the start is searched too, on the same sums as the rate.
        shifts = range(-decim, decim + 1, 2) if synced else range(1)
        if nsym is None:
            piece, base = seg, 0
        else:
            base = max(0, int(start) - decim)
            piece = seg[base : base + int(np.ceil(nsym * sps * 1.01)) + 2 * decim + 2]
        # Mixing is the expensive part and does not depend on the symbol
        # rate or timing, so do it once per sync and try every candidate on
        # the sums.
        yp, ym = _tone_sums(piece, deviation, sample_rate, cfo_hz=cfo_hz)
        rates = _rate_candidates() if refine_rate else np.zeros(1)
        best: tuple[float, np.ndarray[Any, Any], np.ndarray[Any, Any],
                    np.ndarray[Any, Any], float, int] | None = None
        for shift in shifts:
            at = start + shift - base
            if at < 0:
                continue
            for delta in rates:
                trial = sps * (1.0 + float(delta))
                soft, matched, unmatched = _ml_from_sums(yp, ym, float(at), trial, nsym)
                if soft.size == 0:
                    continue
                # Judge on the part that is certainly packet. Past the
                # shortest packet's end there may be only noise, which scores
                # alike under every candidate and just dilutes the comparison.
                confidence = float(np.abs(soft[:judge]).mean())
                if best is None or confidence > best[0]:
                    best = (confidence, soft, matched, unmatched, trial, shift)
        if best is None:
            continue
        _conf, soft, matched, unmatched, trial_sps, shift = best
        start += shift
        # soft is already in transmitted polarity: its sign is the tone the
        # symbol was sent on, and a data 1 is the higher tone. The sync
        # template is written in logical polarity, so a normal (inverted)
        # on-air packet matches it at polarity -1 — that says which template
        # matched, and must not be used to flip the bits.
        bits = "".join("1" if v > 0 else "0" for v in soft)
        if synced:
            keep = _packet_symbols(bits, polarity)
            bits, soft, matched, unmatched = (bits[:keep], soft[:keep],
                                              matched[:keep], unmatched[:keep])
            busy_until = start + (keep - 8) * trial_sps
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


def _packet_symbols(bits: str, polarity: int) -> int:
    """How many symbols of a synced burst belong to its own packet.

    Read from the flags byte, so a standard packet is not followed by the
    first part of whatever came next on the air -- which the parser would
    find and report a second time, with the wrong timestamp. When the first
    frame did not survive, the extended length is kept: better a tail than
    a truncated packet.
    """
    from .manchester import invert_bits
    from .packet import EXT_LEN, FLAG_EXT, FRAME_BITS, MARKER_OFFSET, STD_LEN, decode_frames

    logical = invert_bits(bits) if polarity < 0 else bits
    data, _ = decode_frames(logical, HEADER_SYMBOL_IN_TEMPLATE + MARKER_OFFSET)
    frames = STD_LEN if data and not data[0] & FLAG_EXT else EXT_LEN
    # A few symbols of pad: parse_bits wants to see the frame end cleanly.
    return HEADER_SYMBOL_IN_TEMPLATE + MARKER_OFFSET + FRAME_BITS * frames + 8


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
    hot = _envelope(x, sample_rate / baud) > squelch
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
