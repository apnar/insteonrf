"""PLM wire format — convert decoded RF packets to and from the frames a
PowerLinc Modem hands its host.

Why this exists: insteon-mqtt's protocol layer reads ``02 50`` (standard) and
``02 51`` (extended) frames off the modem's serial link. To let the RF
listeners act as extra antennas for it (``Doc/MESH-PLAN.md``) a decoded RF
:class:`~insteonrf.packet.Packet` has to be presented in exactly that form,
and PLM frames read back off ``insteon/raw/rx`` have to come back the other
way so both paths can be compared with one key.

The two formats carry the same fields in a different order::

    RF  standard   flags, to(3), from(3), cmd1, cmd2, crc, pad
    PLM standard   02 50 from(3) to(3) flags cmd1 cmd2

    RF  extended   flags, to(3), from(3), cmd1, cmd2, d1..d13, dcrc, crc, pad
    PLM extended   02 51 from(3) to(3) flags cmd1 cmd2 d1..d14

Three things are not a straight remap:

* **Byte order.** RF addresses go out low byte first; PLM addresses are high
  byte first (``Address.wire`` vs ``Address.bytes``).
* **Group broadcasts swap the two address slots.** For a direct message the
  RF frame is ``to`` then ``from``; for a group broadcast it is the sender
  first and the destination second. Both slots are always a full three-byte
  address in wire order, so the conversion is a swap and not a special
  encoding.

  This is worth being precise about, because the obvious reading — that a
  group broadcast carries "the group number then ``00 00``" — is only true of
  plain group On/Off. The destination is a real Insteon ``to_addr`` whose
  *low* byte is the group, and other messages use the upper bytes: an
  **ALL-Link Cleanup Status Report** (``cmd1 = 0x06``) reports the original
  command there, so ``11.01.01`` means "On, group 1". insteon-mqtt reads the
  group back out of ``to_addr.ids[2]``, which is why carrying all three bytes
  through matters. 27 of 223 live captures were cleanup status reports, and
  zeroing those bytes lost the command being reported on.
* **The CRCs.** RF carries a packet CRC (and a data CRC on extended
  messages); PLM frames carry neither, because the modem has already checked
  them. They are verified here and dropped, never forwarded.

The flags byte is bit-identical in both formats and is copied through
verbatim rather than re-derived, so a message type this module has never seen
still round-trips.

The extended ``d14`` slot and the RF "data CRC" byte are the same byte: on
i2cs devices it *is* the extended checksum, which is why
:func:`~insteonrf.packet.ext_crc` computes it. On older i2 devices it is
ordinary data. Either way it is carried through untouched.
"""

from __future__ import annotations

from typing import Any

from .packet import (
    EXT_CRC_INDEX,
    EXT_DATA_CRC_INDEX,
    FLAG_BCAST,
    FLAG_EXT,
    FLAG_GROUP,
    STD_CRC_INDEX,
    Address,
    Packet,
    pkt_crc,
)

#: Serial framing byte that starts every PLM message.
PLM_STX = 0x02
#: PLM message code for a received standard-length Insteon message.
INP_STANDARD = 0x50
#: PLM message code for a received extended Insteon message.
INP_EXTENDED = 0x51

#: Total frame length including ``02`` and the message code.
STD_FRAME_LEN = 11
EXT_FRAME_LEN = 25

#: Number of user-data bytes in an extended PLM frame (``d1``..``d14``).
EXT_PLM_DATA_LEN = 14

FRAME_LENS = {INP_STANDARD: STD_FRAME_LEN, INP_EXTENDED: EXT_FRAME_LEN}


class PlmFormatError(ValueError):
    """A packet cannot be represented as a PLM frame, or a frame is malformed."""


# --------------------------------------------------------------------------- RF -> PLM


def to_plm_bytes(pkt: Packet, *, validate: bool = True) -> bytes:
    """Render an RF packet as the PLM frame a modem would have produced.

    With ``validate`` (the default) the packet must be self-consistent before
    it is converted: the CRC has to match, the frame-index counters must not
    have been rejected, and enough bytes must be present. That check is the
    last line of defence before these bytes reach a real protocol stack — an
    8-bit CRC matches one random candidate in 256, which is exactly why
    :attr:`~insteonrf.packet.Packet.index_ok` exists and is consulted here.

    Raises :class:`PlmFormatError` if the packet cannot be converted.
    """
    extended = pkt.extended
    need = (EXT_CRC_INDEX if extended else STD_CRC_INDEX) + 1
    if len(pkt.data) < need:
        raise PlmFormatError(
            f"{'extended' if extended else 'standard'} packet needs {need} bytes, got {len(pkt.data)}"
        )

    if validate:
        if pkt.index_ok is False:
            raise PlmFormatError("frame-index counters were rejected; these bytes are not a packet")
        if not pkt.hops_ok:
            # hops-left above max-hops is a flags byte no transmitter emits,
            # and it is the signature of a false header lock inside the tail
            # of a real capture whose counters and CRC both passed by luck.
            # See Packet.hops_ok. This is the last gate before a protocol
            # stack, so it refuses rather than warns.
            raise PlmFormatError(
                f"impossible hop fields (hops_left {pkt.hops_left} > max_hops "
                f"{pkt.max_hops}); these bytes are not a packet"
            )
        if pkt.crc_ok is False:
            raise PlmFormatError(f"packet CRC mismatch (got {pkt.crc:#04x}, want {pkt.calc_crc:#04x})")
        if pkt.crc_ok is None:
            raise PlmFormatError("packet CRC byte is missing")

    flags = pkt.flags_byte
    slot1 = Address.from_wire(pkt.data[1:4]).bytes
    slot2 = Address.from_wire(pkt.data[4:7]).bytes
    # A group broadcast leads with the sender; everything else leads with the
    # destination. The PLM frame always wants "from" then "to".
    from3, to3 = (slot1, slot2) if pkt.is_group_broadcast else (slot2, slot1)

    code = INP_EXTENDED if extended else INP_STANDARD
    frame = [PLM_STX, code, *from3, *to3, flags, pkt.data[7], pkt.data[8]]
    if extended:
        # RF d1..d13 then the data-CRC byte == PLM d1..d14.
        frame += pkt.data[9 : EXT_DATA_CRC_INDEX + 1]

    expect = FRAME_LENS[code]
    if len(frame) != expect:
        raise PlmFormatError(f"built a {len(frame)}-byte frame, expected {expect}")
    return bytes(frame)


# --------------------------------------------------------------------------- PLM -> RF


def from_plm_bytes(raw: bytes | bytearray | memoryview) -> Packet:
    """Rebuild an RF-layout :class:`~insteonrf.packet.Packet` from a PLM frame.

    Used on the receiving side of ``insteon/raw/rx`` so messages the PLM heard
    can be keyed the same way as messages the listeners heard. The RF CRCs are
    recomputed rather than invented, so the result is self-consistent; the
    extended data-CRC slot keeps whatever ``d14`` the device actually sent.
    """
    buf = bytes(raw)
    if len(buf) < 2:
        raise PlmFormatError("frame is too short to have a message code")
    if buf[0] != PLM_STX:
        raise PlmFormatError(f"frame does not start with {PLM_STX:#04x}")
    code = buf[1]
    if code not in FRAME_LENS:
        raise PlmFormatError(f"message code {code:#04x} is not an inbound Insteon message")
    if len(buf) < FRAME_LENS[code]:
        raise PlmFormatError(f"{code:#04x} frame needs {FRAME_LENS[code]} bytes, got {len(buf)}")

    from_addr = Address(list(buf[2:5]))
    to_bytes = list(buf[5:8])
    flags = buf[8]
    cmd1, cmd2 = buf[9], buf[10]
    extended = code == INP_EXTENDED

    # The flags byte says extended; the message code must agree or one of them
    # is wrong and we would build a packet of the wrong length.
    if bool(flags & FLAG_EXT) != extended:
        raise PlmFormatError(
            f"message code {code:#04x} disagrees with the extended bit in flags {flags:#04x}"
        )

    data = [flags]
    to_wire = Address(to_bytes).wire
    if (flags & (FLAG_BCAST | FLAG_GROUP)) == (FLAG_BCAST | FLAG_GROUP):
        data += [*from_addr.wire, *to_wire]
    else:
        data += [*to_wire, *from_addr.wire]
    data += [cmd1, cmd2]
    if extended:
        data += list(buf[11 : 11 + EXT_PLM_DATA_LEN])
    data.append(pkt_crc(data))
    data += Packet._pad(len(data), extended)
    return Packet(data)


# --------------------------------------------------------------------------- identity


def message_key(pkt: Packet) -> tuple[Any, ...]:
    """Hop-insensitive identity of a message, for suppressing duplicates.

    Two copies of one transmission differ only in the hops-left field (and
    therefore in the CRC computed over it), so those are masked out. Everything
    else is included.

    This is deliberately **stricter** than insteon-mqtt's own
    ``InpStandard.__eq__``, which compares only ``from_addr``, ``flags``,
    ``group``, ``cmd1`` and ``cmd2`` — it does not look at ``to_addr`` at all.
    Being stricter is the safe direction: an extra injection that upstream
    considers a duplicate is dropped by its ``_is_duplicate`` and costs
    nothing, whereas a looser key would suppress a message the PLM never
    actually heard, which is the one failure this whole path exists to
    prevent.
    """
    data = list(pkt.data[: pkt.crc_index] if len(pkt.data) > pkt.crc_index else pkt.data)
    data[0] &= ~0x0C  # mask hops-left
    return tuple(data)


def plm_message_key(raw: bytes | bytearray | memoryview) -> tuple[Any, ...]:
    """:func:`message_key` for a PLM frame, via :func:`from_plm_bytes`."""
    return message_key(from_plm_bytes(raw))


__all__ = [
    "EXT_FRAME_LEN",
    "EXT_PLM_DATA_LEN",
    "FRAME_LENS",
    "INP_EXTENDED",
    "INP_STANDARD",
    "PLM_STX",
    "PlmFormatError",
    "STD_FRAME_LEN",
    "from_plm_bytes",
    "message_key",
    "plm_message_key",
    "to_plm_bytes",
]
