"""Insteon RF packet codec.

Wire layout (see ``Doc/pkt_format.md``)::

    byte 0      flags
    byte 1-3    "to" address, low byte first   (all-link broadcast: sender)
    byte 4-6    "from" address, low byte first (all-link broadcast: group, 00, 00)
    byte 7      cmd1
    byte 8      cmd2
    byte 9      packet CRC                       -- standard packets
    byte 9-21   13 bytes user data               -- extended packets
    byte 22     data checksum                    -- extended packets
    byte 23     packet CRC                       -- extended packets
    ...         pad bytes (00 00 AA for standard, AA... for extended)

Each byte goes on the air as a 28-bit frame: the marker ``11`` followed by
Manchester-coded 5-bit index and 8-bit data, both LSB first. The first byte
carries index 31; the rest count down to 0 (from 11 for standard packets,
from 30 for extended ones). Everything is inverted before transmission, so
a receiver may see either polarity — :func:`parse_bits` accepts both.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import cmds
from .manchester import ManchesterError, invert_bits, manchester_decode, manchester_encode

# The marker of the first frame plus the surrounding preamble/index bits.
START_HEADER = "1100111010101010"
START_HEADER_INV = invert_bits(START_HEADER)
MARKER_OFFSET = 5  # position of the first ``11`` frame marker within START_HEADER
FRAME_BITS = 28  # '11' + 26 Manchester bits
PREAMBLE = "0101010101"
TRAILER = "01" * 24

FLAG_BCAST = 0x80
FLAG_GROUP = 0x40
FLAG_ACK = 0x20
FLAG_EXT = 0x10

MESSAGE_TYPES = (
    "Direct",
    "ACK of Direct",
    "Group Cleanup Direct",
    "ACK of Group Cleanup",
    "Broadcast",
    "NAK of Direct",
    "Group Broadcast",
    "NAK of Group Cleanup",
)

STD_LEN = 13  # 10 bytes + 00 00 AA pad
EXT_LEN = 32  # 24 bytes + 8 pad bytes
STD_CRC_INDEX = 9
EXT_CRC_INDEX = 23
EXT_DATA_CRC_INDEX = 22


# --------------------------------------------------------------------------- CRCs

_CRC_TABLE = (0x00, 0x30, 0x60, 0x50, 0xC0, 0xF0, 0xA0, 0x90,
              0x80, 0xB0, 0xE0, 0xD0, 0x40, 0x70, 0x20, 0x10)


def pkt_crc(data: Sequence[int]) -> int:
    """Packet CRC over bytes 0-8 (standard) or 0-22 (extended).

    Equivalent to ``r ^= b; r ^= ((r ^ (r << 1)) & 0x0F) << 4`` per byte.
    """
    n = EXT_CRC_INDEX if data[0] & FLAG_EXT else STD_CRC_INDEX
    r = 0
    for b in data[:n]:
        r ^= b
        r ^= _CRC_TABLE[r & 0x0F]
    return r


def ext_crc(data: Sequence[int]) -> int:
    """Extended-data checksum: two's complement of the sum of bytes 7-21."""
    return (-sum(data[7:EXT_DATA_CRC_INDEX])) & 0xFF


# --------------------------------------------------------------------------- addresses

def parse_addr(addr: str | Sequence[int] | int) -> list[int]:
    """Return an address as 3 bytes, high byte first.

    Accepts ``"16.3F.E5"``, ``"16:3F:E5"``, ``"163FE5"``, an int, or a 3-item sequence.
    """
    if isinstance(addr, int):
        if not 0 <= addr <= 0xFFFFFF:
            raise ValueError(f"address out of range: {addr:#x}")
        return [(addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF]
    if isinstance(addr, str):
        hexs = addr.replace(".", "").replace(":", "").replace(" ", "")
        if len(hexs) != 6:
            raise ValueError(f"bad Insteon address {addr!r}")
        return [int(hexs[i : i + 2], 16) for i in (0, 2, 4)]
    if len(addr) != 3:
        raise ValueError(f"bad Insteon address {addr!r}")
    return [int(b) & 0xFF for b in addr]


def addr_to_wire(addr) -> list[int]:
    """Address → the 3 bytes as they appear on the wire (low byte first)."""
    return parse_addr(addr)[::-1]


def wire_to_addr(b: Sequence[int]) -> str:
    """3 wire bytes (low byte first) → ``"16.3F.E5"``."""
    return f"{b[2]:02X}.{b[1]:02X}.{b[0]:02X}"


# --------------------------------------------------------------------------- packet

@dataclass
class Packet:
    """A decoded or generated Insteon RF packet, in wire byte order."""

    data: list[int]
    bits: str = field(default="", repr=False)
    timestamp: float | None = field(default=None, repr=False)
    complete: bool = True  # frame index counted down to 0

    # ---- construction ----------------------------------------------------

    @classmethod
    def build(
        cls,
        src,
        dst=None,
        *,
        group: int | None = None,
        cmd1: int = 0,
        cmd2: int = 0,
        ext_data: Iterable[int] | None = None,
        extended: bool | None = None,
        bcast: bool = False,
        ack: bool = False,
        max_hops: int = 3,
        hops_left: int = 3,
        crc: int | None = None,
        pad: bool = True,
    ) -> "Packet":
        """Assemble a packet from its fields.

        ``dst`` is the destination address for direct messages; ``group`` makes
        an all-link (group) message instead — the sender goes in the "to" slot
        and the group number in the "from" slot, which is what devices send.
        """
        if (dst is None) == (group is None):
            raise ValueError("exactly one of dst or group is required")
        ext_data = list(ext_data or [])
        if extended is None:
            extended = bool(ext_data)
        if len(ext_data) > 13:
            raise ValueError("extended data is at most 13 bytes")

        flags = (max_hops & 3) | ((hops_left & 3) << 2)
        if extended:
            flags |= FLAG_EXT
        if ack:
            flags |= FLAG_ACK
        if group is not None:
            flags |= FLAG_GROUP
        if bcast:
            flags |= FLAG_BCAST

        data = [flags]
        if group is not None:
            data += addr_to_wire(src) + [group & 0xFF, 0, 0]
        else:
            data += addr_to_wire(dst) + addr_to_wire(src)
        data += [cmd1 & 0xFF, cmd2 & 0xFF]

        if extended:
            data += ext_data + [0] * (13 - len(ext_data))
            data.append(ext_crc(data))
        data.append(pkt_crc(data) if crc is None else crc & 0xFF)
        if pad:
            data += cls._pad(len(data), extended)
        return cls(data)

    @classmethod
    def from_wire(cls, data: Iterable[int], *, pad: bool = False, crc: bool = False) -> "Packet":
        """Wrap raw wire bytes; optionally append the packet CRC and/or pad bytes."""
        data = [int(b) & 0xFF for b in data]
        if len(data) < 1:
            raise ValueError("empty packet")
        extended = bool(data[0] & FLAG_EXT)
        if crc:
            if len(data) != (EXT_CRC_INDEX if extended else STD_CRC_INDEX):
                raise ValueError("crc=True needs exactly 9 (std) or 23 (ext) bytes")
            data.append(pkt_crc(data))
        if pad:
            data += cls._pad(len(data), extended)
        return cls(data)

    @staticmethod
    def _pad(length: int, extended: bool) -> list[int]:
        if extended:
            return [0xAA] * max(0, EXT_LEN - length)
        n = STD_LEN - length
        return ([0] * (n - 1) + [0xAA]) if n > 0 else []

    # ---- fields ----------------------------------------------------------

    @property
    def flags(self) -> int:
        return self.data[0]

    @property
    def extended(self) -> bool:
        return bool(self.flags & FLAG_EXT)

    @property
    def bcast(self) -> bool:
        return bool(self.flags & FLAG_BCAST)

    @property
    def group_msg(self) -> bool:
        return bool(self.flags & FLAG_GROUP)

    @property
    def ack(self) -> bool:
        return bool(self.flags & FLAG_ACK)

    @property
    def max_hops(self) -> int:
        return self.flags & 3

    @property
    def hops_left(self) -> int:
        return (self.flags >> 2) & 3

    @property
    def msg_type(self) -> int:
        return self.flags >> 5

    @property
    def msg_type_name(self) -> str:
        return MESSAGE_TYPES[self.msg_type]

    @property
    def is_group_broadcast(self) -> bool:
        return self.bcast and self.group_msg

    @property
    def to_addr(self) -> str | None:
        """Destination address, or the sender for group broadcasts (wire slot 1)."""
        return wire_to_addr(self.data[1:4]) if len(self.data) >= 4 else None

    @property
    def from_addr(self) -> str | None:
        if len(self.data) < 7 or self.is_group_broadcast:
            return None
        return wire_to_addr(self.data[4:7])

    @property
    def group(self) -> int | None:
        """All-link group number for group broadcasts (wire byte 4)."""
        return self.data[4] if self.is_group_broadcast and len(self.data) >= 5 else None

    @property
    def cmd1(self) -> int | None:
        return self.data[7] if len(self.data) > 7 else None

    @property
    def cmd2(self) -> int | None:
        return self.data[8] if len(self.data) > 8 else None

    @property
    def cmd_name(self) -> str | None:
        if self.cmd1 is None:
            return None
        return cmds.lookup(self.cmd1, self.cmd2, extended=self.extended, bcast=self.bcast)

    @property
    def ext_data(self) -> list[int] | None:
        return self.data[9:22] if self.extended and len(self.data) >= 22 else None

    @property
    def crc_index(self) -> int:
        return EXT_CRC_INDEX if self.extended else STD_CRC_INDEX

    @property
    def crc(self) -> int | None:
        """The packet CRC as received, if that byte is present."""
        return self.data[self.crc_index] if len(self.data) > self.crc_index else None

    @property
    def calc_crc(self) -> int | None:
        return pkt_crc(self.data) if len(self.data) >= self.crc_index else None

    @property
    def crc_ok(self) -> bool | None:
        return None if self.crc is None else self.crc == self.calc_crc

    @property
    def ext_crc_ok(self) -> bool | None:
        if not self.extended or len(self.data) <= EXT_DATA_CRC_INDEX:
            return None
        return self.data[EXT_DATA_CRC_INDEX] == ext_crc(self.data)

    @property
    def valid(self) -> bool:
        """True when the packet is long enough and its CRC(s) match."""
        return bool(self.crc_ok) and self.ext_crc_ok is not False

    @property
    def time_str(self) -> str:
        ts = _dt.datetime.fromtimestamp(self.timestamp) if self.timestamp else _dt.datetime.now()
        return ts.strftime("%H:%M:%S.%f")[:-3]

    # ---- encoding --------------------------------------------------------

    def to_bits(self, *, invert: bool = True) -> str:
        """Framed, Manchester-coded bit string ready for an FSK modulator.

        ``invert=True`` (the default) yields on-air polarity. The result is
        padded to a multiple of 8 bits so it packs into bytes.
        """
        out = [manchester_encode(PREAMBLE)]
        idx = 31
        for i, byte in enumerate(self.data):
            if i == 1:
                idx = 30 if self.extended else 11
            out.append("11")
            out.append(manchester_encode(f"{idx:05b}"[::-1] + f"{byte:08b}"[::-1]))
            idx -= 1
        out.append(manchester_encode(TRAILER))
        bits = "".join(out)
        if len(bits) % 8:
            bits += manchester_encode(TRAILER)[: 8 - len(bits) % 8]
        return invert_bits(bits) if invert else bits

    # ---- rendering -------------------------------------------------------

    def hex_line(self) -> str:
        """Compact ``flags : to : from : cmd...`` dump in wire order."""
        h = [f"{b:02X}" for b in self.data]
        parts = [h[0], ":", " ".join(h[1:4]), ":", " ".join(h[4:7]), ":", " ".join(h[7:23])]
        if len(h) > 23:
            parts += [":", " ".join(h[23:])]
        return " ".join(parts)

    def summary(self) -> str:
        """The classic one-line output: hex dump plus ``crc XX`` (``CRC`` when it mismatches)."""
        line = self.hex_line()
        if self.calc_crc is None:
            return line
        width = 106 if self.extended else 48
        tag = "crc" if self.crc_ok else "CRC"
        return f"{line:<{width}} {tag} {self.calc_crc:02X}"

    def describe(self) -> str:
        """Multi-line human-readable decode."""
        lines = [self.summary()]
        if self.is_group_broadcast:
            who = f"{self.to_addr} group {self.group}"
        else:
            who = f"{self.from_addr} -> {self.to_addr}"
        detail = f"{self.msg_type_name}: {who}"
        if self.cmd1 is not None:
            detail += f"  {self.cmd_name} (0x{self.cmd1:02X}"
            detail += f" 0x{self.cmd2:02X})" if self.cmd2 is not None else ")"
        detail += f"  hops {self.hops_left}/{self.max_hops}"
        lines.append("    " + detail)
        if self.ext_data is not None:
            lines.append("    data: " + " ".join(f"{b:02X}" for b in self.ext_data)
                         + ("" if self.ext_crc_ok else "  (data checksum mismatch)"))
        if not self.complete:
            lines.append("    (truncated)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- decoding

def decode_frames(bits: str, start: int = 0) -> tuple[list[int], list[int]]:
    """Decode consecutive 28-bit frames starting at ``start``.

    Returns ``(bytes, indexes)``; stops at the first bad marker or Manchester
    error, or when the index reaches 0.
    """
    out: list[int] = []
    idx: list[int] = []
    i = start
    while bits[i : i + 2] == "11":
        try:
            dm = manchester_decode(bits[i + 2 : i + FRAME_BITS])
        except ManchesterError:
            break
        if len(dm) < 13:
            break
        idx.append(int(dm[:5][::-1], 2))
        out.append(int(dm[5:13][::-1], 2))
        i += FRAME_BITS
        if idx[-1] == 0:
            break
    return out, idx


def find_headers(bits: str) -> tuple[str, list[int]]:
    """Normalise polarity and return ``(bits, [header offsets])``.

    Picks whichever polarity of the start header occurs first.
    """
    a = bits.find(START_HEADER)
    b = bits.find(START_HEADER_INV)
    if a == -1 and b == -1:
        return bits, []
    if a == -1 or (b != -1 and b < a):
        bits = invert_bits(bits)
    offsets = []
    pos = bits.find(START_HEADER)
    while pos != -1:
        offsets.append(pos)
        pos = bits.find(START_HEADER, pos + len(START_HEADER))
    return bits, offsets


def parse_bits(line: str, timestamp: float | None = None, *, min_bytes: int = 4) -> list[Packet]:
    """Extract every packet from one ASCII bit string (any polarity, any alignment).

    Fragments shorter than ``min_bytes`` are dropped.
    """
    line = line.strip()
    bits, offsets = find_headers(line)
    packets = []
    for pos in offsets:
        data, idx = decode_frames(bits, pos + MARKER_OFFSET)
        if len(data) < min_bytes:
            continue
        end = pos + MARKER_OFFSET + FRAME_BITS * len(data)
        packets.append(Packet(data, bits[pos:end], timestamp, complete=bool(idx) and idx[-1] == 0))
    return packets


def iter_bit_lines(lines: Iterable[str]):
    """Yield ``(kind, text)`` for a pipeline stream: ``'meta'`` for ``#`` lines, ``'bits'`` for data."""
    for raw in lines:
        s = raw.rstrip("\n")
        if not s.strip():
            continue
        if s.startswith("#"):
            yield "meta", s
        elif s[0] in "01":
            yield "bits", s.strip()
        else:
            yield "junk", s


def dump_frames(line: str) -> str:
    """Verbose per-frame breakdown of a bit string, for debugging demodulator output."""
    bits, offsets = find_headers(line.strip())
    out = [f"len {len(bits)} bits, {len(offsets)} start header(s)" + (" (inverted input)" if bits != line.strip() else "")]
    for n, pos in enumerate(offsets):
        out.append(f"-- packet {n} at bit {pos}")
        i = pos + MARKER_OFFSET
        data: list[int] = []
        j = 0
        while bits[i : i + 2] == "11":
            raw = bits[i + 2 : i + FRAME_BITS]
            try:
                dm = manchester_decode(raw)
            except ManchesterError as err:
                out.append(f"   {j:2d} @{i:5d} 11 {raw}  {err}")
                break
            if len(dm) < 13:
                out.append(f"   {j:2d} @{i:5d} 11 {raw}  short frame")
                break
            idx = int(dm[:5][::-1], 2)
            byte = int(dm[5:13][::-1], 2)
            data.append(byte)
            note = ""
            if j == 0:
                p = Packet(data)
                note = f"flags: {p.msg_type_name}, ext={int(p.extended)}, hops {p.hops_left}/{p.max_hops}"
            elif j == 7:
                note = cmds.lookup(byte, extended=bool(data[0] & FLAG_EXT), bcast=bool(data[0] & FLAG_BCAST))
            elif j == (EXT_CRC_INDEX if data[0] & FLAG_EXT else STD_CRC_INDEX):
                c = pkt_crc(data)
                note = "crc OK" if c == byte else f"CRC mismatch, expected {c:02X}"
            elif data[0] & FLAG_EXT and j == EXT_DATA_CRC_INDEX:
                c = ext_crc(data)
                note = "data checksum OK" if c == byte else f"data checksum mismatch, expected {c:02X}"
            out.append(f"   {j:2d} @{i:5d} 11 {raw} idx={idx:2d} {byte:02X}  {note}")
            i += FRAME_BITS
            j += 1
            if idx == 0:
                break
        if data:
            out.append("   bytes: " + " ".join(f"{b:02X}" for b in data))
    return "\n".join(out)
