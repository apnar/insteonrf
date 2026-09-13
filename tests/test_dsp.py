"""Modulator/demodulator tests — no hardware, no compiler needed for most of them."""

import pathlib
import subprocess

import numpy as np
import pytest

from insteonrf.dsp import demodulate_fsk2, iq_bursts, modulate_fsk2
from insteonrf.packet import Packet, parse_bits

DATA = pathlib.Path(__file__).resolve().parent / "data"
CAPTURE = pathlib.Path(__file__).resolve().parent.parent / "Dat" / "41802513110D2711018C00.dat"

STD = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F, cmd2=0x00)
EXT = Packet.build("2B.93.07", "29.4E.52", cmd1=0x2F, ext_data=[0, 0, 0x0F, 0xFF, 1, 2, 3])


def c_decode(binary, samples, *, signed=True, sample_rate=2_400_000, extra=()):
    argv = [str(binary), "-U" if signed else "-u", "-s", str(sample_rate), *extra]
    out = subprocess.run(argv, input=samples.tobytes(), capture_output=True, check=True)
    return [p.data for line in out.stdout.decode().splitlines() for p in parse_bits(line)]


def py_decode(samples, **kw):
    return [p.data for bits in demodulate_fsk2(samples, **kw) for p in parse_bits(bits)]


# ---- modulator shape ----


def test_sample_format_and_length():
    bits = STD.to_bits()
    signed = modulate_fsk2(bits)
    assert signed.dtype == np.int8 and signed.size % 2 == 0
    # 2 ms preamble + packet + 1 ms trailer at 9124 baud, 2.4 Msps.
    expected = (len(bits) + round(0.002 * 9124) + round(0.001 * 9124)) * 2_400_000 / 9124
    assert abs(signed.size / 2 - expected) < 2
    unsigned = modulate_fsk2(bits, signed=False)
    assert unsigned.dtype == np.uint8
    # The same waveform, offset by 128.
    assert np.abs(unsigned.astype(int) - 128 - signed.astype(int)).max() <= 1


def test_amplitude_and_ramp():
    s = modulate_fsk2(STD.to_bits(), amplitude=120).astype(int)
    mag = np.hypot(s[0::2], s[1::2])
    assert 118 <= mag[len(mag) // 2] <= 121  # constant envelope in the middle
    assert mag[0] < 10 and mag[-1] < 10  # ramped at both ends


def test_rejects_bad_arguments():
    with pytest.raises(ValueError):
        modulate_fsk2("0101", amplitude=200)
    with pytest.raises(ValueError):
        modulate_fsk2("0102")
    with pytest.raises(ValueError):
        modulate_fsk2("0101", sample_rate=1000)


def test_fractional_bit_clock_does_not_slip():
    """263.05 samples per bit: an extended packet must still decode at the end."""
    data = py_decode(modulate_fsk2(EXT.to_bits()))
    assert EXT.data in data


# ---- round trips through both demodulators ----


@pytest.mark.parametrize("pkt", [STD, EXT], ids=["std", "ext"])
def test_roundtrip_numpy(pkt):
    assert pkt.data in py_decode(modulate_fsk2(pkt.to_bits()))


@pytest.mark.parametrize("pkt", [STD, EXT], ids=["std", "ext"])
def test_roundtrip_c_demod(fsk2_demod, pkt):
    """fsk2_demod must decode our samples without -i (same bit polarity)."""
    assert pkt.data in c_decode(fsk2_demod, modulate_fsk2(pkt.to_bits()))


def test_roundtrip_unsigned(fsk2_demod):
    samples = modulate_fsk2(STD.to_bits(), signed=False)
    assert STD.data in c_decode(fsk2_demod, samples, signed=False)
    assert STD.data in py_decode(samples, signed=False)


@pytest.mark.parametrize("rate", [2_048_000, 1_000_000])
def test_roundtrip_other_sample_rates(fsk2_demod, rate):
    samples = modulate_fsk2(STD.to_bits(), sample_rate=rate)
    assert STD.data in c_decode(fsk2_demod, samples, sample_rate=rate)
    assert STD.data in py_decode(samples, sample_rate=rate)


def test_polarity_is_transmitted_polarity(fsk2_demod):
    """The demodulated bits are the bits we fed in, not their complement."""
    bits = STD.to_bits()
    out = subprocess.run([str(fsk2_demod), "-U"], input=modulate_fsk2(bits).tobytes(),
                         capture_output=True, check=True).stdout.decode()
    assert bits[24:220] in out
    assert bits[24:220] in demodulate_fsk2(modulate_fsk2(bits))[0]


@pytest.mark.parametrize("sigma", [5, 20, 35])
def test_roundtrip_with_noise(sigma):
    rng = np.random.default_rng(1234)
    clean = modulate_fsk2(STD.to_bits()).astype(np.float64)
    noisy = np.clip(np.rint(clean + rng.normal(0, sigma, clean.size)), -127, 127).astype(np.int8)
    assert STD.data in py_decode(noisy), f"failed at sigma={sigma}"


# ---- the real capture ----


def test_numpy_demod_matches_c_on_real_capture(fsk2_demod):
    raw = CAPTURE.read_bytes()
    py = demodulate_fsk2(raw, signed=True)
    c = [line for line in subprocess.run([str(fsk2_demod), "-U"], input=raw, capture_output=True,
                                         check=True).stdout.decode().splitlines() if line.strip()]
    assert len(py) == len(c) == 1
    # Both find the same packet, and the bits agree where the two bursts overlap.
    assert [p.summary() for p in parse_bits(py[0])] == [p.summary() for p in parse_bits(c[0])]
    i, j = c[0].find(py[0][:16]), 0
    assert i >= 0
    overlap = min(len(c[0]) - i, len(py[0]))
    assert c[0][i : i + overlap] == py[0][j : j + overlap]


def test_numpy_demod_decodes_real_capture():
    bits = demodulate_fsk2(CAPTURE.read_bytes(), signed=True)
    pkts = [p for b in bits for p in parse_bits(b)]
    assert len(pkts) == 1
    assert pkts[0].summary() == "41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C"


def test_demod_ignores_silence():
    assert demodulate_fsk2(np.zeros(4096, dtype=np.int8)) == []
    assert demodulate_fsk2(b"") == []


# ---- burst splitting ----


def test_iq_bursts_finds_one_burst():
    samples = modulate_fsk2(STD.to_bits())
    silence = np.zeros(20_000, dtype=np.int8)
    stream = np.concatenate([silence, samples, silence, samples, silence])
    bursts = iq_bursts(stream)
    assert len(bursts) == 2
    for b in bursts:
        assert STD.data in py_decode(b)


def test_iq_bursts_on_real_capture():
    """The capture is one already-clipped burst, so it comes back whole."""
    bursts = iq_bursts(CAPTURE.read_bytes())
    assert len(bursts) == 1
    assert bursts[0].size >= 0.99 * CAPTURE.stat().st_size
    assert py_decode(bursts[0])
