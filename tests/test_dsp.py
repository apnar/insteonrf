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


# ---- the receive chain has to keep up with busy air ----


def test_fft_correlation_matches_the_direct_form():
    from insteonrf.dsp import _xcorr_valid

    rng = np.random.default_rng(3)
    d = rng.normal(size=40_000).astype(np.float32)
    t = rng.normal(size=900).astype(np.float32)
    assert np.allclose(_xcorr_valid(d, t), np.correlate(d, t, mode="valid"), atol=1e-2)


def test_running_envelope_matches_the_convolution_it_replaced():
    from insteonrf.dsp import _envelope

    rng = np.random.default_rng(4)
    x = rng.normal(size=20_000) + 1j * rng.normal(size=20_000)
    sps = 2_400_000 / 9124
    win = int(round(sps))
    want = np.convolve(np.abs(x), np.ones(win) / win, mode="same")
    assert np.allclose(_envelope(x, sps), want, atol=1e-4)


def test_every_packet_in_one_squelch_run_is_found():
    """A run can hold a whole exchange; find_sync used to stop at four."""
    from insteonrf.dsp import demodulate_bursts

    pkts = [Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F, cmd2=i) for i in range(8)]
    # No gaps at all: one continuous run of carrier.
    wave = np.concatenate([modulate_fsk2(p.to_bits(), trailer_s=0) for p in pkts])
    got = [q.data for b in demodulate_bursts(wave) for q in parse_bits(b.bits) if q.crc_ok]
    assert sorted(got) == sorted(p.data for p in pkts)


def test_bursts_are_stamped_from_t0():
    from insteonrf.dsp import demodulate_bursts

    lead = np.zeros(2 * 24_000, dtype=np.int8)  # 10 ms of silence
    (b,) = demodulate_bursts(np.concatenate([lead, modulate_fsk2(STD.to_bits())]), t0=50.0)
    assert b.timestamp == pytest.approx(50.0 + b.start / 2_400_000)
    assert 50.010 < b.timestamp < 50.015


def test_an_extended_payload_does_not_yield_a_second_sync():
    """Syncs are spaced by the shortest packet, and an extended payload is
    long enough to hold a false one: nothing real starts inside a packet."""
    from insteonrf.dsp import demodulate_bursts

    bursts = demodulate_bursts(modulate_fsk2(EXT.to_bits()))
    assert len(bursts) == 1
    assert EXT.data in [p.data for p in parse_bits(bursts[0].bits)]


def test_a_standard_burst_stops_at_its_own_packet():
    """Running on into the next slot made the parser report that packet
    twice, once with the wrong time."""
    from insteonrf.dsp import demodulate_bursts

    nxt = Packet.build("2B.93.07", "29.4E.52", cmd1=0x19)
    wave = np.concatenate([modulate_fsk2(STD.to_bits(), trailer_s=0), modulate_fsk2(nxt.to_bits())])
    per_burst = [[p.data for p in parse_bits(b.bits) if p.crc_ok] for b in demodulate_bursts(wave)]
    assert per_burst == [[STD.data], [nxt.data]]


@pytest.mark.parametrize("cfo_hz", [-20_000, 9_000, 20_000])
def test_carrier_offset_does_not_hide_the_sync_under_noise(cfo_hz):
    """Every cheap-crystal device is kilohertz off. With sync found on the
    full-rate discriminator, a weak packet 9 kHz off decoded 15% of the time
    at 23.7 dB symbol SNR (6 kHz: 100%) -- the discriminator is below the FM
    threshold there. Low-pass first, then take the phase step."""
    from insteonrf.dsp import demodulate_bursts

    rng = np.random.default_rng(5)
    clean = modulate_fsk2(STD.to_bits(), amplitude=40).astype(np.float64)
    n = np.arange(clean.size // 2)
    iq = (clean[0::2] + 1j * clean[1::2]) * np.exp(2j * np.pi * cfo_hz * n / 2_400_000)
    ok = 0
    for _ in range(10):
        noisy = np.empty_like(clean)
        noisy[0::2] = iq.real + rng.normal(0, 30, iq.size)
        noisy[1::2] = iq.imag + rng.normal(0, 30, iq.size)
        s = np.clip(np.rint(noisy), -127, 127).astype(np.int8)
        bursts = demodulate_bursts(s)
        ok += any(p.data == STD.data for b in bursts for p in parse_bits(b.bits) if p.crc_ok)
        assert all(abs(b.cfo_hz - cfo_hz) < 3_000 for b in bursts if b.header_index is not None)
    assert ok >= 9


@pytest.mark.parametrize("cfo_hz", [0, 20_000, -35_000, 52_000])
def test_a_weak_packet_is_synced_and_decoded_whatever_its_offset(cfo_hz):
    """At ~13 dB symbol SNR the phase discriminator finds no sync at all; a
    weak packet at zero offset still decoded, by luck, through the no-sync
    fallback (which assumes zero offset), and one 20 kHz off never did. The
    tone-energy sync finds it, and the template gives its offset."""
    from insteonrf.dsp import demodulate_bursts

    rng = np.random.default_rng(9)
    clean = modulate_fsk2(STD.to_bits(), amplitude=12).astype(np.float64)
    n = np.arange(clean.size // 2)
    iq = (clean[0::2] + 1j * clean[1::2]) * np.exp(2j * np.pi * cfo_hz * n / 2_400_000)
    ok = 0
    for _ in range(10):
        noisy = np.empty_like(clean)
        noisy[0::2] = iq.real + rng.normal(0, 30, iq.size)
        noisy[1::2] = iq.imag + rng.normal(0, 30, iq.size)
        bursts = demodulate_bursts(np.clip(np.rint(noisy), -127, 127).astype(np.int8))
        synced = [b for b in bursts if b.header_index is not None]
        assert synced, "no sync found"
        assert abs(synced[0].cfo_hz - cfo_hz) < 1_000
        ok += any(p.data == STD.data for b in synced for p in parse_bits(b.bits) if p.crc_ok)
    assert ok >= 8


def test_a_burst_reports_how_much_of_it_was_clipped():
    """Next to the PLM the V4 clipped 40% of samples; that has to be visible
    per burst, or a bad placement looks like a bad demodulator."""
    from insteonrf.dsp import demodulate_bursts

    (clean,) = demodulate_bursts(modulate_fsk2(STD.to_bits(), amplitude=60))
    assert clean.clipped == 0.0
    loud = (modulate_fsk2(STD.to_bits(), amplitude=127).astype(np.int16) * 3).clip(-127, 127)
    (hot,) = demodulate_bursts(loud.astype(np.int8))
    assert hot.clipped > 0.5
