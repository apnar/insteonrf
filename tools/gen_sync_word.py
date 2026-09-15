#!/usr/bin/env python3
"""Emit the SX1262 sync word for Insteon RF, derived from ``Packet.to_bits()``.

Do not work this out by hand. The on-air stream is inverted, LSB-first and
Manchester-coded, and a sign error baked into firmware is invisible — the
radio simply never syncs and reports nothing, which looks exactly like "no
traffic". ``Packet.to_bits()`` is validated against real devices, so the
constant is taken from there and pinned by ``tests/test_sync_word.py``.

What is invariant at the start of every packet:

* the preamble, a repeating ``0110`` cell (``0x66``), inverted on air to
  ``1001`` (``0x99``);
* then ``START_HEADER``, 16 bits covering the preamble tail, the literal
  ``11`` frame marker and the Manchester-coded frame index 31 — which every
  packet begins with, whatever its contents. Inverted on air that is
  ``0x3155``, the same word the CC1111 dongle syncs on.

Nothing after that is data-independent, so the sync word stops there.

Usage::

    python tools/gen_sync_word.py            # the default 32-bit word
    python tools/gen_sync_word.py --bits 24
    python tools/gen_sync_word.py --both     # both polarities
"""

from __future__ import annotations

import argparse
import sys

from insteonrf.packet import (
    START_HEADER,
    START_HEADER_INV,
    Address,
    Packet,
)

#: The SX126x sync word register block is 8 bytes.
MAX_SYNC_BITS = 64


def on_air_prefix(nbits: int) -> str:
    """The first ``nbits`` invariant on-air bits, ending at the start header.

    Built from a real packet's bit stream so the preamble phase is whatever
    the transmitter actually produces, rather than what we assume.
    """
    bits = Packet.build(Address("29.4E.52"), Address("2B.93.07"), cmd1=0x0D, cmd2=0x00).to_bits()
    pos = bits.find(START_HEADER_INV)
    polarity = "inverted"
    if pos == -1:
        pos = bits.find(START_HEADER)
        polarity = "plain"
    if pos == -1:
        raise SystemExit("no start header in to_bits() output")
    end = pos + len(START_HEADER)
    header = bits[pos:end]

    # The preamble is a period-4 square wave, so extending it backwards means
    # tiling the 4-bit cell that immediately precedes the header -- taking the
    # cell from the real stream keeps the phase right. Tiling from an
    # arbitrary offset instead silently produces a shifted word, which a radio
    # will never match while looking exactly like "no traffic".
    if pos < 4:
        raise SystemExit("not enough preamble in to_bits() to read the cell phase")
    cell = bits[pos - 4 : pos]
    need = nbits - len(header)
    if need < 0:
        raise SystemExit(f"--bits must be at least {len(header)}")
    if need % len(cell):
        raise SystemExit("--bits must leave a whole number of 4-bit preamble cells")
    sys.stderr.write(
        f"# start header at {pos} ({polarity} polarity), preamble cell {cell}\n"
    )
    return cell * (need // len(cell)) + header


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bits", type=int, default=32,
                   help="sync word length in bits, a multiple of 8 (default %(default)s)")
    p.add_argument("--both", action="store_true", help="also print the complement")
    a = p.parse_args(argv)
    if a.bits % 8 or not 8 <= a.bits <= MAX_SYNC_BITS:
        p.error(f"--bits must be a multiple of 8 between 8 and {MAX_SYNC_BITS}")

    word = on_air_prefix(a.bits)
    nbytes = a.bits // 8

    def show(label: str, bits: str) -> None:
        value = int(bits, 2)
        hexed = f"{value:0{nbytes * 2}X}"
        as_bytes = ", ".join(f"0x{hexed[i:i + 2]}" for i in range(0, len(hexed), 2))
        print(f"{label:10} 0x{hexed}  ({a.bits} bits)")
        print(f"{'':10} bits {bits}")
        print(f"{'':10} bytes {{{as_bytes}}}")

    show("on air", word)
    if a.both:
        show("inverted", "".join("1" if c == "0" else "0" for c in word))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
