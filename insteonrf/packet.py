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
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from . import cmds
from .cmds import Command
from .manchester import ManchesterError, invert_bits, manchester_decode, manchester_encode

# The marker of the first frame plus the surrounding preamble/index bits.
START_HEADER = "1100111010101010"
START_HEADER_INV = invert_bits(START_HEADER)
MARKER_OFFSET = 5  # position of the first ``11`` frame marker within START_HEADER

#: 32-bit on-air sync word for a receiver that has a sync-word detector: four
#: preamble cells followed by ``START_HEADER`` inverted. Ends in ``0x3155``,
#: which is exactly what the CC1111 dongle syncs on — an independent check
#: that the polarity and phase are right. Generate with
#: ``tools/gen_sync_word.py``; ``tests/test_sync_word.py`` pins it, so the
#: ESPHome firmware and this package cannot drift apart.
ON_AIR_SYNC_32 = 0x33333155
ON_AIR_SYNC_32_BITS = 32
FRAME_BITS = 28  # '11' + 26 Manchester bits
PREAMBLE = "0101010101"
TRAILER = "01" * 24

FLAG_BCAST = 0x80
FLAG_GROUP = 0x40
FLAG_ACK = 0x20
FLAG_EXT = 0x10

STD_LEN = 13  # 10 bytes + 00 00 AA pad
EXT_LEN = 32  # 24 bytes + 8 pad bytes
STD_CRC_INDEX = 9
EXT_CRC_INDEX = 23
EXT_DATA_CRC_INDEX = 22
EXT_DATA_LEN = 13


class MsgType(IntEnum):
    """The message type encoded in the top three flag bits."""

    DIRECT = 0
    DIRECT_ACK = 1
    GROUP_CLEANUP = 2
    GROUP_CLEANUP_ACK = 3
    BROADCAST = 4
    DIRECT_NAK = 5
    GROUP_BROADCAST = 6
    GROUP_CLEANUP_NAK = 7

    @property
    def label(self) -> str:
        return MESSAGE_TYPES[int(self)]

    def __str__(self) -> str:
        return self.label


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


class Address:
    """An Insteon device address, printed as ``16.3F.E5``.

    Accepts ``"16.3F.E5"``, ``"16:3F:E5"``, ``"163FE5"``, an int, another
    :class:`Address`, or a 3-item high-byte-first sequence. Compares equal to
    (and hashes like) its string form, so it drops into code and dicts that
    still use address strings.
    """

    __slots__ = ("_value",)

    _value: int

    def __init__(self, addr: Address | str | int | Sequence[int]):
        if isinstance(addr, Address):
            value = addr._value
        elif isinstance(addr, bool):
            raise TypeError("bool is not an Insteon address")
        elif isinstance(addr, int):
            if not 0 <= addr <= 0xFFFFFF:
                raise ValueError(f"address out of range: {addr:#x}")
            value = addr
        elif isinstance(addr, str):
            hexs = addr.replace(".", "").replace(":", "").replace(" ", "")
            if len(hexs) != 6:
                raise ValueError(f"bad Insteon address {addr!r}")
            try:
                value = int(hexs, 16)
            except ValueError:
                raise ValueError(f"bad Insteon address {addr!r}") from None
        else:
            b = list(addr)
            if len(b) != 3 or any(not 0 <= int(x) <= 0xFF for x in b):
                raise ValueError(f"bad Insteon address {addr!r}")
            value = (int(b[0]) << 16) | (int(b[1]) << 8) | int(b[2])
        self._value = value

    # ---- alternate constructors ----

    @classmethod
    def from_wire(cls, b: Sequence[int]) -> Address:
        """Build from the 3 bytes as they appear on the wire (low byte first)."""
        if len(b) < 3:
            raise ValueError("need 3 wire bytes")
        return cls([int(b[2]) & 0xFF, int(b[1]) & 0xFF, int(b[0]) & 0xFF])

    # ---- views ----

    @property
    def value(self) -> int:
        """The address as a 24-bit int."""
        return self._value

    @property
    def bytes(self) -> tuple[int, int, int]:
        """High byte first, the order the address is written in."""
        v = self._value
        return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)

    @property
    def wire(self) -> tuple[int, int, int]:
        """Low byte first, the order the address goes on the air."""
        return self.bytes[::-1]

    # ---- protocol ----

    def __str__(self) -> str:
        return "{:02X}.{:02X}.{:02X}".format(*self.bytes)

    def __repr__(self) -> str:
        return f"Address('{self}')"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Address):
            return self._value == other._value
        if isinstance(other, str):
            try:
                return self._value == Address(other)._value
            except ValueError:
                return False
        return NotImplemented  # type: ignore[unreachable]

    def __hash__(self) -> int:
        return hash(str(self))

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


# --------------------------------------------------------------------------- flags


@dataclass(frozen=True)
class Flags:
    """The flags byte, unpacked."""

    msg_type: MsgType = MsgType.DIRECT
    extended: bool = False
    hops_left: int = 3
    max_hops: int = 3

    @classmethod
    def from_byte(cls, b: int) -> Flags:
        return cls(
            msg_type=MsgType(b >> 5),
            extended=bool(b & FLAG_EXT),
            hops_left=(b >> 2) & 3,
            max_hops=b & 3,
        )

    def to_byte(self) -> int:
        return ((int(self.msg_type) & 7) << 5
                | (FLAG_EXT if self.extended else 0)
                | ((self.hops_left & 3) << 2)
                | (self.max_hops & 3))

    @property
    def bcast(self) -> bool:
        return bool(self.to_byte() & FLAG_BCAST)

    @property
    def group(self) -> bool:
        return bool(self.to_byte() & FLAG_GROUP)

    @property
    def ack(self) -> bool:
        return bool(self.to_byte() & FLAG_ACK)

    def __str__(self) -> str:
        s = self.msg_type.label
        if self.extended:
            s += ", extended"
        return f"{s}, hops {self.hops_left}/{self.max_hops}"


# --------------------------------------------------------------------------- packet


@dataclass
class Packet:
    """A decoded or generated Insteon RF packet, in wire byte order."""

    data: list[int]
    bits: str = field(default="", repr=False, compare=False)
    timestamp: float | None = field(default=None, repr=False, compare=False)
    complete: bool = field(default=True, compare=False)  # frame index counted down to 0
    #: How many bits :mod:`insteonrf.recover` had to flip to make the CRCs pass.
    corrected: int = field(default=0, repr=False, compare=False)
    #: Estimated SNR of the burst this came from, when a soft demodulator saw it.
    snr_db: float | None = field(default=None, repr=False, compare=False)
    #: Receiver signal strength in dBm, when the radio reports it.
    rssi_dbm: float | None = field(default=None, repr=False, compare=False)
    #: For an ACK/NAK: whether the query it answers was extended, when
    #: :class:`insteonrf.context.CommandTracker` could work it out. ``None``
    #: means unknown, and an ambiguous command is then reported as both.
    ack_of_extended: bool | None = field(default=None, repr=False, compare=False)
    #: Whether the frame-index counters ran as the protocol requires. ``False``
    #: means these bytes are not a packet however well the CRC happened to
    #: match — an 8-bit CRC matches one random candidate in 256.
    index_ok: bool | None = field(default=None, repr=False, compare=False)

    # ---- construction ----------------------------------------------------

    @classmethod
    def build(
        cls,
        src: Address | str | int,
        dst: Address | str | int | None = None,
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
    ) -> Packet:
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
        if len(ext_data) > EXT_DATA_LEN:
            raise ValueError(f"extended data is at most {EXT_DATA_LEN} bytes")

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
            data += list(Address(src).wire) + [group & 0xFF, 0, 0]
        else:
            assert dst is not None  # guaranteed by the check above
            data += list(Address(dst).wire) + list(Address(src).wire)
        data += [cmd1 & 0xFF, cmd2 & 0xFF]

        if extended:
            data += ext_data + [0] * (EXT_DATA_LEN - len(ext_data))
            data.append(ext_crc(data))
        data.append(pkt_crc(data) if crc is None else crc & 0xFF)
        if pad:
            data += cls._pad(len(data), extended)
        return cls(data)

    @classmethod
    def from_wire(cls, data: Iterable[int], *, pad: bool = False, crc: bool = False) -> Packet:
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

    @classmethod
    def parse(cls, raw: bytes | bytearray | memoryview) -> Packet:
        """Wrap raw wire bytes exactly as given (the inverse of ``bytes(pkt)``)."""
        return cls(list(bytes(raw)))

    @staticmethod
    def _pad(length: int, extended: bool) -> list[int]:
        if extended:
            return [0xAA] * max(0, EXT_LEN - length)
        n = STD_LEN - length
        return ([0] * (n - 1) + [0xAA]) if n > 0 else []

    def __bytes__(self) -> bytes:
        return bytes(self.data)

    def __len__(self) -> int:
        return len(self.data)

    # ---- fields ----------------------------------------------------------

    @property
    def flags_byte(self) -> int:
        return self.data[0]

    @property
    def flags(self) -> Flags:
        return Flags.from_byte(self.data[0])

    @property
    def extended(self) -> bool:
        return bool(self.flags_byte & FLAG_EXT)

    @property
    def bcast(self) -> bool:
        return bool(self.flags_byte & FLAG_BCAST)

    @property
    def group_msg(self) -> bool:
        return bool(self.flags_byte & FLAG_GROUP)

    @property
    def ack(self) -> bool:
        return bool(self.flags_byte & FLAG_ACK)

    @property
    def max_hops(self) -> int:
        return self.flags_byte & 3

    @property
    def hops_left(self) -> int:
        return (self.flags_byte >> 2) & 3

    @property
    def hops_ok(self) -> bool:
        """Whether the hop fields are ones a real transmission can carry.

        A device sends with ``hops_left == max_hops`` and every repeater
        decrements hops-left, so hops-left can never exceed max-hops. Nothing
        on the air says otherwise: across 18,939 received messages from this
        network's own devices it held 99.96% of the time, and the handful of
        exceptions were bit errors.

        It is worth checking because the other guards do not cover the flags
        byte. A capture is longer than one packet -- a message, the start of
        its next hop, then padding -- and :func:`parse_bits` looks for the
        start header at every offset in either polarity, so a false lock in
        that tail occasionally yields a short frame whose 5-bit counters
        descend correctly *and* whose 8-bit CRC matches by luck (one in 256
        per candidate, against ~19,000 real messages a day each offering
        several offsets). Those phantoms then repeat forever, because the
        same byte pattern recurs. One family of them, ``AC/AD/AE/AF.4C.1E``,
        spent eight days looking like a neighbour's device: 82 clean decodes,
        every one within a second of one of this network's own messages
        (median 31 ms), 38% of them with hops-left above max-hops, and an
        "address" whose top byte cycled while its low two bytes never moved.
        """
        return self.hops_left <= self.max_hops

    @property
    def msg_type(self) -> MsgType:
        return MsgType(self.flags_byte >> 5)

    @property
    def msg_type_name(self) -> str:
        return self.msg_type.label

    @property
    def is_group_broadcast(self) -> bool:
        return self.bcast and self.group_msg

    @property
    def to_addr(self) -> Address | None:
        """Destination address, or the sender for group broadcasts (wire slot 1)."""
        return Address.from_wire(self.data[1:4]) if len(self.data) >= 4 else None

    @property
    def from_addr(self) -> Address | None:
        if len(self.data) < 7 or self.is_group_broadcast:
            return None
        return Address.from_wire(self.data[4:7])

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
    def command(self) -> Command | None:
        """``cmd1`` as a :class:`~insteonrf.cmds.Command` when it is a known one."""
        if self.cmd1 is None:
            return None
        try:
            return Command(self.cmd1)
        except ValueError:
            return None

    @property
    def cmd_name(self) -> str | None:
        """The command's name, read from the right table.

        An ACK is a standard message even when it answers an extended command,
        and it echoes the query's ``cmd1`` — so for the handful of numbers that
        mean different things in the two tables, one packet is not enough.
        With ``ack_of_extended`` set (see :mod:`insteonrf.context`) the reply is
        read in the query's table; without it, both meanings are reported
        rather than guessing one.
        """
        if self.cmd1 is None:
            return None
        extended = self.extended
        if self.ack:
            if self.ack_of_extended is not None:
                extended = self.ack_of_extended
            elif cmds.ambiguous(self.cmd1):
                return cmds.both_labels(self.cmd1)
        return cmds.lookup(self.cmd1, self.cmd2, extended=extended, bcast=self.bcast,
                           ack=self.ack)

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

    # ---- serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view of the packet (see the schema in the README)."""
        d: dict[str, Any] = {
            "time": (_dt.datetime.fromtimestamp(self.timestamp).isoformat(timespec="milliseconds")
                     if self.timestamp else None),
            "timestamp": self.timestamp,
            "msg_type": self.msg_type.name,
            "msg_type_name": self.msg_type_name,
            "extended": self.extended,
            "ack": self.ack,
            "broadcast": self.bcast,
            "group_msg": self.group_msg,
            "hops_left": self.hops_left,
            "max_hops": self.max_hops,
            "hops_ok": self.hops_ok,
            "to": str(self.to_addr) if self.to_addr else None,
            "from": str(self.from_addr) if self.from_addr else None,
            "group": self.group,
            "cmd1": self.cmd1,
            "cmd2": self.cmd2,
            "command": self.cmd_name,
            "command_known": (None if self.cmd1 is None else
                              cmds.is_known(self.cmd1, extended=self.extended, bcast=self.bcast)),
            "ext_data": self.ext_data,
            "crc": self.crc,
            "crc_ok": self.crc_ok,
            "ext_crc_ok": self.ext_crc_ok,
            "complete": self.complete,
            "corrected": self.corrected,
            "snr_db": round(self.snr_db, 1) if self.snr_db is not None else None,
            "rssi_dbm": round(self.rssi_dbm, 1) if self.rssi_dbm is not None else None,
            "raw": bytes(self.data).hex().upper(),
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Packet:
        """Rebuild a packet from :meth:`to_dict` output (``raw`` is authoritative)."""
        raw = d.get("raw")
        if raw is None:
            raise ValueError("packet dict needs a 'raw' hex string")
        return cls(list(bytes.fromhex(raw)), timestamp=d.get("timestamp"),
                   complete=bool(d.get("complete", True)))

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


def expected_indexes(extended: bool) -> list[int]:
    """The frame-index sequence a packet of this kind must carry.

    The counter is 31 for the flags byte, then counts down from 11 (standard)
    or 30 (extended) to 0. It carries no information, so it is free error
    detection — 65 bits of it on a standard packet.
    """
    return [31] + list(range(30 if extended else 11, -1, -1))


def indexes_ok(data: Sequence[int], idx: Sequence[int]) -> bool:
    """True when a decoded frame-index sequence is the one the protocol mandates."""
    if not idx or not data:
        return False
    want = expected_indexes(bool(data[0] & FLAG_EXT))
    return list(idx) == want[: len(idx)]


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
        pkt = Packet(data, bits[pos:end], timestamp, complete=bool(idx) and idx[-1] == 0)
        pkt.index_ok = indexes_ok(data, idx)
        packets.append(pkt)
    return packets


def iter_bit_lines(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
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
