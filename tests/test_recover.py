"""Soft-decision framing and CRC-guided repair."""

import numpy as np
import pytest

from insteonrf.dsp import demodulate_bursts, modulate_fsk2
from insteonrf.packet import Packet, parse_bits
from insteonrf.recover import (
    expected_indexes,
    recover_from_burst,
    recover_packets,
)

STD = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F, cmd2=0x00)
EXT = Packet.build("2B.93.07", "29.4E.52", cmd1=0x2F, ext_data=[0, 0, 0x0F, 0xFF, 1, 2, 3])


def burst_of(pkt, **kw):
    return demodulate_bursts(modulate_fsk2(pkt.to_bits(), **kw))[0]


def flip_logical(soft, header_index, frame, bit, confidence=0.05):
    """Flip one logical bit in a soft array, as a marginal symbol would.

    A logical bit is the *difference* of its Manchester pair, so swapping the
    pair flips the decision; scaling it down makes it the low-confidence
    position a repair search should reach for.
    """
    base = header_index + 5 + frame * 28 + 2 + 2 * bit
    a, b = float(soft[base]), float(soft[base + 1])
    soft[base], soft[base + 1] = b * confidence, a * confidence


def bits_of(soft):
    return "".join("1" if v > 0 else "0" for v in soft)


# ---- the frame-index constraint ----


def test_expected_indexes():
    assert expected_indexes(False) == [31] + list(range(11, -1, -1))
    assert expected_indexes(True) == [31] + list(range(30, -1, -1))
    assert len(expected_indexes(False)) == 13 and len(expected_indexes(True)) == 32


# ---- decoding without soft information (what the rfcat dongle gives) ----


@pytest.mark.parametrize("pkt", [STD, EXT], ids=["std", "ext"])
def test_recover_from_hard_bits(pkt):
    recs = recover_packets(pkt.to_bits())
    assert len(recs) == 1
    assert recs[0].packet.data == pkt.data
    assert recs[0].corrected == 0 and recs[0].index_errors == 0


def test_recover_accepts_either_polarity():
    from insteonrf.manchester import invert_bits

    for bits in (STD.to_bits(), invert_bits(STD.to_bits())):
        recs = recover_packets(bits)
        assert len(recs) == 1 and recs[0].packet.data == STD.data


def test_recover_finds_nothing_in_noise():
    rng = np.random.default_rng(7)
    for _ in range(20):
        bits = "".join(rng.choice(["0", "1"]) for _ in range(600))
        assert recover_packets(bits) == []


# ---- soft decisions ----


@pytest.mark.parametrize("pkt", [STD, EXT], ids=["std", "ext"])
def test_recover_from_burst(pkt):
    recs = recover_from_burst(burst_of(pkt))
    assert len(recs) == 1
    assert recs[0].packet.data == pkt.data
    assert recs[0].packet.snr_db and recs[0].packet.snr_db > 20


def test_soft_combining_beats_hard_decisions():
    """One corrupted symbol per Manchester pair must still decode.

    Manchester sends each logical bit as 01 or 10, so the pair's difference is
    what matters: nudging one symbol of a pair the wrong way cannot flip the
    decision unless it overwhelms its partner.
    """
    burst = burst_of(STD)
    soft = burst.soft.copy()
    # Weaken (but do not flip) the first symbol of many pairs.
    soft[::2] *= 0.1
    recs = recover_packets(burst.bits, soft, header_index=burst.header_index)
    assert len(recs) == 1 and recs[0].packet.data == STD.data


def test_repair_fixes_a_single_flipped_bit():
    burst = burst_of(STD)
    soft = burst.soft.copy()
    flip_logical(soft, burst.header_index, frame=4, bit=7)  # a data bit
    broken = bits_of(soft)

    assert recover_packets(broken, soft, header_index=burst.header_index, repair=False) == []
    recs = recover_packets(broken, soft, header_index=burst.header_index)
    assert len(recs) == 1
    assert recs[0].packet.data == STD.data and recs[0].corrected == 1


def test_repair_fixes_two_flipped_bits():
    burst = burst_of(STD)
    soft = burst.soft.copy()
    flip_logical(soft, burst.header_index, frame=2, bit=6)
    flip_logical(soft, burst.header_index, frame=7, bit=9)
    recs = recover_packets(bits_of(soft), soft, header_index=burst.header_index)
    assert len(recs) == 1
    assert recs[0].packet.data == STD.data and recs[0].corrected == 2


def test_repair_gives_up_beyond_max_flips():
    burst = burst_of(STD)
    soft = burst.soft.copy()
    for frame, bit in ((1, 5), (3, 6), (5, 7), (9, 8)):
        flip_logical(soft, burst.header_index, frame, bit)
    out = recover_packets(bits_of(soft), soft, header_index=burst.header_index, max_flips=2)
    assert all(r.packet.data != STD.data for r in out)


def test_repair_leaves_a_good_packet_alone():
    burst = burst_of(STD)
    recs = recover_packets(burst.bits, burst.soft, header_index=burst.header_index)
    assert len(recs) == 1 and recs[0].corrected == 0


def test_repair_refuses_when_the_frame_grid_is_wrong():
    """The index counters are the guard that keeps an 8-bit CRC honest."""
    burst = burst_of(STD)
    soft = burst.soft.copy()
    # Break the frame numbering with confident wrong values, and break a data
    # bit too so the CRC fails and the repair search is reached.
    for frame in range(1, 6):
        flip_logical(soft, burst.header_index, frame, bit=0, confidence=1.0)
        flip_logical(soft, burst.header_index, frame, bit=2, confidence=1.0)
    flip_logical(soft, burst.header_index, frame=6, bit=7)
    out = recover_packets(bits_of(soft), soft, header_index=burst.header_index)
    assert out == [] or all(r.packet.data != STD.data for r in out)


def test_recover_two_packets_in_one_bit_string():
    a = Packet.build("13.25.80", "16.3F.E5", cmd1=0x11, cmd2=0xFF)
    b = Packet.build("16.3F.E5", "13.25.80", cmd1=0x11, cmd2=0xFF, ack=True)
    recs = recover_packets(a.to_bits() + "0110" * 8 + b.to_bits())
    assert [r.packet.data for r in recs] == [a.data, b.data]


def test_recover_matches_parse_bits_on_the_real_capture():
    import pathlib

    cap = pathlib.Path(__file__).resolve().parent.parent / "Dat" / "41802513110D2711018C00.dat"
    burst = demodulate_bursts(cap.read_bytes())[0]
    recs = recover_from_burst(burst)
    assert len(recs) == 1
    assert recs[0].packet.summary() == (
        "41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C")
    assert [p.data for p in parse_bits(burst.bits)] == [recs[0].packet.data]


# ---- the index invariant as a false-positive guard ----


def test_index_ok_flag_from_parse_bits():
    from insteonrf.packet import expected_indexes as exp
    from insteonrf.packet import indexes_ok

    pkts = parse_bits(STD.to_bits())
    assert len(pkts) == 1 and pkts[0].index_ok is True
    assert indexes_ok([0x0F], [31, 11, 10]) is True
    assert indexes_ok([0x0F], [31, 12]) is False        # standard starts at 11
    assert indexes_ok([0x1F], [31, 30, 29]) is True     # extended starts at 30
    assert indexes_ok([0x0F], []) is False
    assert exp(False)[1] == 11 and exp(True)[1] == 30


def swap_pair(bits: str, pos: int) -> str:
    """Swap a Manchester pair, flipping its logical bit but staying legal."""
    b = list(bits)
    b[pos], b[pos + 1] = b[pos + 1], b[pos]
    return "".join(b)


def test_wrong_frame_counters_are_rejected_even_with_a_matching_crc():
    """The CRC covers the data bytes only, so it cannot see a broken counter.

    This is the false-positive class the index check exists for: a well-formed
    Manchester frame sequence whose CRC matches but whose frame numbering is
    impossible. ``parse_bits`` reports it (and flags it); ``recover`` refuses.
    """
    bits = STD.to_bits()
    header = bits.find("".join("1" if c == "0" else "0" for c in "1100111010101010"))
    if header < 0:  # logical polarity
        header = bits.find("1100111010101010")
    assert header >= 0
    for frame in (2, 3, 5):
        for j in (0, 1):
            bits = swap_pair(bits, header + 5 + frame * 28 + 2 + 2 * j)

    found = parse_bits(bits)
    assert found, "the frame sequence should still parse"
    assert found[0].crc_ok is True, "the CRC does not cover the index bits"
    assert found[0].index_ok is False
    assert recover_packets(bits) == []


def test_recover_rejects_random_bits():
    """Manchester legality already makes a chance CRC match vanishingly rare."""
    rng = np.random.default_rng(4242)
    for _ in range(500):
        bits = "".join(rng.choice(["0", "1"], size=560))
        assert recover_packets(bits) == []
