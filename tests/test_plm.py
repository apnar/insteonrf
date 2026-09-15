"""RF <-> PLM wire format (``insteonrf.plm``).

The interesting tests here are the ones marked ``imqtt``: they hand the bytes
we generate to insteon-mqtt's *own* parser and check it recovers the fields we
started from. Asserting against a hand-written expectation only proves we
agree with ourselves; round-tripping through the real consumer is what makes
injection safe.
"""

from __future__ import annotations

import pytest

from insteonrf.packet import Address, Packet, pkt_crc
from insteonrf.plm import (
    EXT_FRAME_LEN,
    INP_EXTENDED,
    INP_STANDARD,
    STD_FRAME_LEN,
    PlmFormatError,
    from_plm_bytes,
    message_key,
    to_plm_bytes,
)

PLM = "2B.93.07"
DEV = "29.4E.52"


def std(**kw) -> Packet:
    kw.setdefault("cmd1", 0x0D)
    kw.setdefault("cmd2", 0x00)
    return Packet.build(PLM, DEV, **kw)


def grp(group: int = 1, **kw) -> Packet:
    kw.setdefault("cmd1", 0x11)
    kw.setdefault("cmd2", 0xFF)
    return Packet.build(DEV, group=group, bcast=True, **kw)


def ext(**kw) -> Packet:
    kw.setdefault("cmd1", 0x2F)
    kw.setdefault("cmd2", 0x00)
    kw.setdefault("ext_data", [0x00, 0x01, 0x0F, 0xFF, 0x08] + [0] * 8)
    return Packet.build(PLM, DEV, **kw)


ALL_SHAPES = {
    "direct": std(),
    "direct-ack": std(ack=True, cmd2=0x02),
    "direct-nak": std(bcast=True, ack=True, cmd2=0xFF),
    "group-bcast": grp(),
    "group-bcast-g0": grp(group=0),
    "group-cleanup": Packet.build(DEV, PLM, cmd1=0x11, cmd2=0x01, group=None),
    "extended": ext(),
    "hops-0": std(hops_left=0, max_hops=0),
    "hops-mixed": std(hops_left=1, max_hops=3),
}


# --------------------------------------------------------------------------- shape


def test_standard_frame_layout():
    p = std(hops_left=2, max_hops=3)
    f = to_plm_bytes(p)
    assert len(f) == STD_FRAME_LEN
    assert f[0] == 0x02 and f[1] == INP_STANDARD
    # from == the RF "from" slot, to == the RF "to" slot, both high byte first.
    assert tuple(f[2:5]) == Address(PLM).bytes
    assert tuple(f[5:8]) == Address(DEV).bytes
    assert f[8] == p.flags_byte
    assert f[9] == 0x0D and f[10] == 0x00


def test_extended_frame_layout():
    p = ext()
    f = to_plm_bytes(p)
    assert len(f) == EXT_FRAME_LEN
    assert f[1] == INP_EXTENDED
    # d1..d14 == the 13 RF data bytes plus the RF data-CRC byte.
    assert list(f[11:25]) == p.data[9:23]
    assert f[24] == p.data[22], "d14 must be the RF data-CRC slot"


def test_group_broadcast_moves_the_sender_and_encodes_the_group():
    p = grp(group=42)
    f = to_plm_bytes(p)
    # Sender comes out of the RF "to" slot into the PLM "from" slot...
    assert tuple(f[2:5]) == Address(DEV).bytes
    # ...and "to" becomes 00.00.<group>, where insteon-mqtt reads the group.
    assert tuple(f[5:8]) == (0x00, 0x00, 42)


def test_group_zero_survives():
    """Group 0 is the phantom all-on. It must not be confused with "no group"."""
    f = to_plm_bytes(grp(group=0))
    assert tuple(f[5:8]) == (0x00, 0x00, 0x00)
    assert from_plm_bytes(f).group == 0


def test_address_byte_order_is_reversed():
    f = to_plm_bytes(std())
    assert tuple(f[2:5]) == (0x2B, 0x93, 0x07)  # written order
    assert tuple(std().data[4:7]) == (0x07, 0x93, 0x2B)  # wire order


# --------------------------------------------------------------------------- round trip


@pytest.mark.parametrize("name", sorted(ALL_SHAPES))
def test_round_trip_preserves_every_field(name):
    p = ALL_SHAPES[name]
    back = from_plm_bytes(to_plm_bytes(p))
    assert back.flags_byte == p.flags_byte
    assert back.msg_type == p.msg_type
    assert back.extended == p.extended
    assert back.to_addr == p.to_addr
    assert back.from_addr == p.from_addr
    assert back.group == p.group
    assert back.cmd1 == p.cmd1
    assert back.cmd2 == p.cmd2
    assert back.crc_ok is True
    if p.extended:
        assert back.data[9:23] == p.data[9:23]


@pytest.mark.parametrize("name", sorted(ALL_SHAPES))
def test_round_trip_preserves_the_dedupe_key(name):
    """What the injector keys on must survive the conversion."""
    p = ALL_SHAPES[name]
    assert message_key(from_plm_bytes(to_plm_bytes(p))) == message_key(p)


def test_hops_do_not_change_the_key_but_do_reach_the_frame():
    a, b = std(hops_left=3), std(hops_left=1)
    assert message_key(a) == message_key(b)
    assert to_plm_bytes(a)[8] != to_plm_bytes(b)[8], "hops must still be carried"


# --------------------------------------------------------------------------- refusals


def test_refuses_a_bad_crc():
    p = std()
    p.data[p.crc_index] ^= 0xFF
    with pytest.raises(PlmFormatError, match="CRC mismatch"):
        to_plm_bytes(p)
    to_plm_bytes(p, validate=False)  # opt out explicitly and it converts


def test_refuses_rejected_frame_indexes():
    """An 8-bit CRC matches 1 in 256 random candidates; index_ok is the guard."""
    p = std()
    p.index_ok = False
    with pytest.raises(PlmFormatError, match="frame-index"):
        to_plm_bytes(p)


def test_refuses_a_truncated_packet():
    with pytest.raises(PlmFormatError, match="needs"):
        to_plm_bytes(Packet(std().data[:6]))


def test_refuses_a_missing_crc():
    p = Packet(std().data[:9])
    with pytest.raises(PlmFormatError):
        to_plm_bytes(p)


@pytest.mark.parametrize(
    "raw, match",
    [
        (b"", "too short"),
        (b"\x03\x50" + b"\x00" * 9, "does not start"),
        (b"\x02\x62" + b"\x00" * 9, "not an inbound"),
        (b"\x02\x50" + b"\x00" * 5, "needs 11"),
        (b"\x02\x51" + b"\x00" * 9, "needs 25"),
    ],
)
def test_from_plm_rejects_malformed_frames(raw, match):
    with pytest.raises(PlmFormatError, match=match):
        from_plm_bytes(raw)


def test_from_plm_rejects_code_flag_disagreement():
    """A 0x50 frame whose flags claim extended would build the wrong length."""
    f = bytearray(to_plm_bytes(std()))
    f[8] |= 0x10
    with pytest.raises(PlmFormatError, match="disagrees"):
        from_plm_bytes(bytes(f))


# --------------------------------------------------------------------------- oracle


@pytest.mark.imqtt
@pytest.mark.parametrize("name", sorted(ALL_SHAPES))
def test_insteon_mqtt_parses_what_we_generate(imqtt_message, name):
    """The real consumer must recover the fields we started from."""
    p = ALL_SHAPES[name]
    raw = to_plm_bytes(p)

    cls = imqtt_message.types[raw[1]]
    assert len(raw) >= cls.fixed_msg_size
    msg = cls.from_bytes(raw)

    assert str(msg.from_addr).upper() == str(p.from_addr or p.to_addr).upper()
    assert msg.flags.hops_left == p.hops_left
    assert msg.flags.max_hops == p.max_hops
    assert msg.flags.is_ext == p.extended
    assert int(msg.flags.type) == int(p.msg_type)
    assert msg.cmd1 == p.cmd1
    assert msg.cmd2 == p.cmd2

    if p.is_group_broadcast:
        # This is the assertion that matters: insteon-mqtt reads the group out
        # of to_addr's low byte, so a wrong remap would silently produce the
        # wrong group rather than an error.
        assert msg.group == p.group
    else:
        assert str(msg.to_addr).upper() == str(p.to_addr).upper()

    if p.extended:
        assert list(msg.data) == p.data[9:23]


@pytest.mark.imqtt
def test_insteon_mqtt_agrees_on_duplicate_identity(imqtt_message):
    """Our hop-masking must match upstream's, or suppression drifts from its dedup."""
    a = imqtt_message.types[0x50].from_bytes(to_plm_bytes(std(hops_left=3)))
    b = imqtt_message.types[0x50].from_bytes(to_plm_bytes(std(hops_left=0)))
    assert a == b, "upstream ignores hops; so does message_key"

    c = imqtt_message.types[0x50].from_bytes(to_plm_bytes(std(cmd2=0x01)))
    assert a != c
    assert message_key(std(hops_left=3)) == message_key(std(hops_left=0))
    assert message_key(std()) != message_key(std(cmd2=0x01))


@pytest.mark.imqtt
def test_expire_time_window_is_zero_at_no_hops(imqtt_message):
    """Documents the trap that makes injector-side suppression mandatory.

    insteon-mqtt's duplicate window is ``hops_left * 0.087``, so a copy that
    arrives with no hops left is never considered a duplicate of anything.
    """
    import time

    now = time.time()
    hop0 = imqtt_message.types[0x50].from_bytes(to_plm_bytes(std(hops_left=0)))
    hop3 = imqtt_message.types[0x50].from_bytes(to_plm_bytes(std(hops_left=3)))
    assert hop0.expire_time - now == pytest.approx(0.0, abs=0.05)
    assert hop3.expire_time - now == pytest.approx(3 * 0.087, abs=0.05)


# --------------------------------------------------------------------------- captures


#: Real bursts taken off the air by the rfcat dongle, one per interesting shape.
#: The group broadcasts are the reason this file exists: the first is a plain
#: group On (to_addr 00.00.01) and the rest are ALL-Link Cleanup Status Reports
#: whose to_addr carries the reported command in its high byte. An earlier
#: version of to_plm_bytes() wrote "group 00 00" and silently dropped that.
LIVE = {
    "direct":            "0542092507932B0D00D90000AA",
    "direct-ack":        "2107932B4209250D026F00",
    "group-cleanup":     "4529015018583F1104C738C1AA",
    "group-cleanup-ack": "6054AE3407932B11015100",
    "group-on-plain":    "CB18583F0400001104F50000AA",
    "cleanup-on-g1":     "CB54AE340101110600E20000AA",
    "cleanup-on-g4":     "CB18583F0401110601370000AA",
    "cleanup-off-g1":    "CB54AE340101130600800000AA",
    "cleanup-off-g4":    "CBAF603F04011306015A0000AA",
}


@pytest.mark.parametrize("name", sorted(LIVE))
def test_live_capture_round_trips(name):
    p = Packet.parse(bytes.fromhex(LIVE[name]))
    assert message_key(from_plm_bytes(to_plm_bytes(p))) == message_key(p)


def test_plain_group_broadcast_really_does_use_00_00():
    """The simple case the old docstring described, kept so the swap stays right."""
    p = Packet.parse(bytes.fromhex(LIVE["group-on-plain"]))
    assert p.is_group_broadcast and p.group == 4 and p.cmd1 == 0x11
    assert tuple(to_plm_bytes(p)[5:8]) == (0x00, 0x00, 0x04)


def test_cleanup_status_report_keeps_the_reported_command():
    """to_addr 11.01.01 means "On, group 1" — the high byte is not padding."""
    p = Packet.parse(bytes.fromhex(LIVE["cleanup-on-g1"]))
    assert p.is_group_broadcast and p.group == 1
    assert p.cmd1 == 0x06, "ALL-Link Cleanup Status Report"
    f = to_plm_bytes(p)
    assert tuple(f[5:8]) == (0x11, 0x01, 0x01)
    assert from_plm_bytes(f).data[4:7] == p.data[4:7]


@pytest.mark.imqtt
@pytest.mark.parametrize("name", sorted(LIVE))
def test_insteon_mqtt_parses_live_captures(imqtt_message, name):
    p = Packet.parse(bytes.fromhex(LIVE[name]))
    raw = to_plm_bytes(p)
    msg = imqtt_message.types[raw[1]].from_bytes(raw)
    assert str(msg.from_addr).upper() == str(p.from_addr or p.to_addr).upper()
    assert msg.cmd1 == p.cmd1 and msg.cmd2 == p.cmd2
    if p.is_group_broadcast:
        assert msg.group == p.group


def test_doc_example_packet_converts(tmp_path):
    """The packet worked through by hand in Doc/pkt_format.md."""
    wire = [0x0B, 0xE5, 0x3F, 0x16, 0x80, 0x25, 0x13, 0x11, 0xBF]
    wire.append(pkt_crc(wire))
    p = Packet.from_wire(wire, pad=True)
    assert p.crc_ok is True
    f = to_plm_bytes(p)
    assert tuple(f[2:5]) == (0x13, 0x25, 0x80)  # from 13.25.80
    assert tuple(f[5:8]) == (0x16, 0x3F, 0xE5)  # to   16.3F.E5
    assert f[8] == 0x0B and f[9] == 0x11 and f[10] == 0xBF
