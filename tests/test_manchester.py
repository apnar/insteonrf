import pytest

from insteonrf.manchester import ManchesterError, invert_bits, manchester_decode, manchester_encode


def test_encode_decode_roundtrip():
    bits = "0110100110001111"
    enc = manchester_encode(bits)
    assert len(enc) == 2 * len(bits)
    assert manchester_decode(enc) == bits


def test_known_vector():
    # from the original gen_packet self-test
    assert manchester_encode("11100111") == "0101011010010101"


def test_invalid_pair_raises():
    with pytest.raises(ManchesterError):
        manchester_decode("0111")


def test_invert():
    assert invert_bits("0011") == "1100"
