"""Pin the SX1262 sync word to what ``Packet.to_bits()`` actually produces.

A wrong sync word is the worst kind of firmware bug: the radio never matches,
reports nothing, and looks exactly like a quiet house. So the constant is
generated from the transmit path that is validated against real devices, and
this test fails if the two ever disagree — including the copy compiled into
the ESPHome component.
"""

from __future__ import annotations

import pathlib
import re

from insteonrf.packet import (
    ON_AIR_SYNC_32,
    ON_AIR_SYNC_32_BITS,
    START_HEADER,
    START_HEADER_INV,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def generated(nbits: int = 32) -> str:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from gen_sync_word import on_air_prefix

    return on_air_prefix(nbits)


def test_constant_matches_the_transmit_path():
    bits = generated(ON_AIR_SYNC_32_BITS)
    assert len(bits) == ON_AIR_SYNC_32_BITS
    assert int(bits, 2) == ON_AIR_SYNC_32


def test_the_sync_word_ends_with_the_inverted_start_header():
    """The same 0x3155 the CC1111 dongle syncs on."""
    bits = f"{ON_AIR_SYNC_32:0{ON_AIR_SYNC_32_BITS}b}"
    assert bits.endswith(START_HEADER_INV)
    assert int(START_HEADER_INV, 2) == 0x3155
    assert int(START_HEADER, 2) == 0xCEAA


def test_the_sync_word_starts_with_whole_preamble_cells():
    bits = f"{ON_AIR_SYNC_32:0{ON_AIR_SYNC_32_BITS}b}"
    preamble = bits[: -len(START_HEADER_INV)]
    assert len(preamble) % 4 == 0
    assert len(set(preamble[i:i + 4] for i in range(0, len(preamble), 4))) == 1, \
        "the preamble must be a single repeating cell, in phase"


def test_a_shorter_word_is_a_suffix_of_the_longer_one():
    """Shortening the sync word must not shift its phase."""
    short = generated(24)
    long = generated(32)
    assert long.endswith(short)


def test_the_esphome_component_uses_the_same_value():
    src = ROOT / "esphome" / "components" / "insteon_rf" / "insteon_rf.h"
    if not src.is_file():
        import pytest

        pytest.skip("ESPHome component not present")
    text = src.read_text()
    found = re.search(r"INSTEON_SYNC_WORD\s*(?:=|\{)\s*(0x[0-9A-Fa-f]+)", text)
    assert found, "insteon_rf.h must define INSTEON_SYNC_WORD"
    assert int(found.group(1), 16) == ON_AIR_SYNC_32
