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


def demodulate_fsk2(
    samples: np.ndarray[Any, Any] | bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    baud: float = DEFAULT_BAUD,
    signed: bool = True,
    squelch: float = 12.0,
    invert: bool = False,
    min_bits: int = 64,
) -> list[str]:
    """Demodulate interleaved 8-bit I/Q into one bit string per burst.

    A quadrature discriminator (``angle(x[n] * conj(x[n-1]))``) gives the
    instantaneous frequency; a magnitude squelch splits the stream into bursts;
    each burst is sliced with a fractional bit clock whose phase comes from the
    first zero crossing of the discriminator output, integrating over the
    middle of each bit rather than sampling it once — which is where this wins
    over ``Src/fsk2_demod.c``: identical output on clean signal (the captures
    in ``Dat/`` and :func:`modulate_fsk2` output decode bit-for-bit the same),
    and roughly seven times more noise tolerance.

    Returns the bursts that yielded at least ``min_bits`` bits.
    """
    x = _to_complex(samples, signed=signed)
    if x.size < 8:
        return []
    mag = np.abs(x)
    # Smooth the envelope over ~1 bit so modulation nulls don't chop a burst up.
    win = max(1, int(round(sample_rate / baud)))
    env = cast("np.ndarray[Any, Any]", np.convolve(mag, np.ones(win) / win, mode="same"))
    hot = env > squelch
    if not hot.any():
        return []

    disc = np.angle(x[1:] * np.conj(x[:-1]))
    sps = sample_rate / baud
    out: list[str] = []
    for start, stop in _runs(hot):
        stop = min(stop, disc.size)
        if stop - start < sps * 4:
            continue
        seg = disc[start:stop]
        bits = _slice_bits(seg, sps, invert=invert)
        if len(bits) >= min_bits:
            out.append(bits)
    return out


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
