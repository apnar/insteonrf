"""Frame-level debugging aids — for staring at demodulator output."""

from __future__ import annotations

from . import cmds
from .manchester import ManchesterError, manchester_decode
from .packet import (
    EXT_CRC_INDEX,
    EXT_DATA_CRC_INDEX,
    FLAG_BCAST,
    FLAG_EXT,
    FRAME_BITS,
    MARKER_OFFSET,
    STD_CRC_INDEX,
    Packet,
    ext_crc,
    find_headers,
    pkt_crc,
)


def dump_frames(line: str) -> str:
    """Verbose per-frame breakdown of a bit string, for debugging demodulator output."""
    bits, offsets = find_headers(line.strip())
    inverted = " (inverted input)" if bits != line.strip() else ""
    out = [f"len {len(bits)} bits, {len(offsets)} start header(s)" + inverted]
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
            out.append(f"   {j:2d} @{i:5d} 11 {raw} idx={idx:2d} {byte:02X}  {_note(data, j)}")
            i += FRAME_BITS
            j += 1
            if idx == 0:
                break
        if data:
            out.append("   bytes: " + " ".join(f"{b:02X}" for b in data))
    return "\n".join(out)


def _note(data: list[int], j: int) -> str:
    """A short explanation of the byte just decoded, given the bytes so far."""
    byte = data[j]
    extended = bool(data[0] & FLAG_EXT)
    if j == 0:
        p = Packet(data)
        return f"flags: {p.msg_type_name}, ext={int(p.extended)}, hops {p.hops_left}/{p.max_hops}"
    if j == 7:
        return cmds.lookup(byte, extended=extended, bcast=bool(data[0] & FLAG_BCAST))
    if j == (EXT_CRC_INDEX if extended else STD_CRC_INDEX):
        c = pkt_crc(data)
        return "crc OK" if c == byte else f"CRC mismatch, expected {c:02X}"
    if extended and j == EXT_DATA_CRC_INDEX:
        c = ext_crc(data)
        return "data checksum OK" if c == byte else f"data checksum mismatch, expected {c:02X}"
    return ""
