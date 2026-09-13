"""Command-name coverage.

The expectations here come from measuring a real network: 257,000 messages in
five months of PLM logs from a 136-device installation (68 dimmers, 18 relay
switches, 13 KeypadLincs, 11 FanLincs, 21 leak sensors, 3 remotes, 2 door
sensors). Every command that traffic actually contained, in the proportion it
contained it, must have a name.
"""

import pytest

from insteonrf import cmds
from insteonrf.packet import Packet

#: (cmd1, extended, broadcast, how often it appeared) from that measurement.
#: The long tail of undocumented high values is deliberately absent: standard
#: Insteon powerline messages carry no CRC, so those were corrupted receptions,
#: not commands. (RF packets do carry a CRC, so they never reach the decoder.)
OBSERVED = [
    (0x13, False, True, 168143), (0x11, False, True, 56421),
    (0x06, False, True, 11185), (0x12, False, True, 613),
    (0x14, False, True, 196), (0x17, False, True, 193), (0x18, False, True, 156),
    (0x13, False, False, 6636), (0x11, False, False, 4976),
    (0x2F, False, False, 3722), (0x0D, False, False, 949),
    (0x2E, False, False, 634), (0x10, False, False, 634),
    (0x01, False, False, 560), (0x19, False, False, 136),
    (0x1F, False, False, 128), (0x09, False, False, 90),
    (0x0A, False, False, 86), (0x0F, False, False, 49),
    (0x03, False, False, 42), (0x02, False, False, 35),
    (0x18, False, False, 33), (0x28, False, False, 25),
    (0x21, False, False, 20), (0x17, False, False, 18),
    (0x16, False, False, 17), (0x15, False, False, 14),
    (0x22, False, False, 11), (0x20, False, False, 11),
    (0x25, False, False, 9), (0x2B, False, False, 8),
    (0x27, False, False, 6), (0x29, False, False, 6),
    (0x23, False, False, 5),
    (0x2F, True, False, 7839), (0x2E, True, False, 286), (0x03, True, False, 1),
]


@pytest.mark.parametrize("cmd1,ext,bcast,count", OBSERVED,
                         ids=[f"{'ext' if e else 'std'}{'-bcast' if b else ''}-{c:02x}"
                              for c, e, b, _ in OBSERVED])
def test_every_observed_command_has_a_name(cmd1, ext, bcast, count):
    assert cmds.is_known(cmd1, extended=ext, bcast=bcast), (
        f"0x{cmd1:02X} appeared {count} times on a real network and has no name")
    assert "Command 0x" not in cmds.lookup(cmd1, extended=ext, bcast=bcast)


def test_observed_traffic_is_fully_named():
    """Weighted by how often each command actually occurs."""
    total = sum(n for *_, n in OBSERVED)
    named = sum(n for c, e, b, n in OBSERVED if cmds.is_known(c, extended=e, bcast=b))
    assert named == total


# ---- the cleanup status report ----


def test_cleanup_status_report():
    """cmd2 is the count of responders that did not answer (verified on 11,184)."""
    assert cmds.lookup(0x06, 0x00, bcast=True) == (
        "ALL-Link Cleanup Status Report: all responders answered")
    assert cmds.lookup(0x06, 0x01, bcast=True).endswith("1 responder did not answer")
    assert cmds.lookup(0x06, 0x03, bcast=True).endswith("3 responders did not answer")
    # Not a broadcast: 0x06 has no standard-table meaning, so it stays unnamed.
    assert not cmds.is_known(0x06)


# ---- broadcast tables fall back to the standard ones ----


def test_broadcast_falls_back_to_the_standard_table():
    """A command that means the same broadcast should not read 'Bcast Command'."""
    for cmd1 in (0x09, 0x0A, 0x19, 0x25, 0x2F, 0x30):
        assert cmds.is_known(cmd1, bcast=True)
        assert cmds.lookup(cmd1, bcast=True) == cmds.lookup(cmd1)
    # Where the broadcast meaning differs, the broadcast table still wins.
    assert cmds.lookup(0x01, bcast=True) == "Set Button Pressed (Responder)"
    assert cmds.lookup(0x01) == "Assign to Group"


def test_extended_broadcast_is_not_a_dead_end():
    assert cmds.is_known(0x2F, extended=True, bcast=True)
    assert cmds.lookup(0x2F, extended=True, bcast=True) == "Read/Write ALL-Link Database"


def test_unknown_commands_still_report_their_class():
    assert cmds.lookup(0xBA) == "Std Command 0xBA"
    assert cmds.lookup(0xBA, bcast=True) == "Bcast Command 0xBA"
    assert cmds.lookup(0xBA, extended=True) == "Ext Command 0xBA"
    assert cmds.lookup(0xBA, extended=True, bcast=True) == "Bcast Ext Command 0xBA"
    assert not cmds.is_known(0xBA)


# ---- cmd2 is a reply, not a sub-command, in an ACK ----


def test_ack_does_not_read_cmd2_as_a_sub_command():
    """In an ACK, cmd2 is the device's answer: an on-level, a version, a byte."""
    assert cmds.lookup(0x20, 0x02) == "Set Operating Flags: LED On"
    assert cmds.lookup(0x20, 0x02, ack=True) == "Set Operating Flags"

    query = Packet.build("2B.93.07", "29.4E.52", cmd1=0x20, cmd2=0x02)
    reply = Packet.build("29.4E.52", "2B.93.07", cmd1=0x20, cmd2=0x02, ack=True)
    assert query.cmd_name == "Set Operating Flags: LED On"
    assert reply.cmd_name == "Set Operating Flags"


def test_status_request_ack_carries_a_level_not_a_sub_command():
    ack = Packet.build("29.4E.52", "2B.93.07", cmd1=0x19, cmd2=0xBF, ack=True)
    assert ack.cmd_name == "Status Request" and ack.cmd2 == 0xBF


def test_command_known_in_the_json():
    good = Packet.build("2B.93.07", "29.4E.52", cmd1=0x11, cmd2=0xFF)
    assert good.to_dict()["command_known"] is True
    bad = Packet.build("2B.93.07", "29.4E.52", cmd1=0xBA)
    d = bad.to_dict()
    assert d["command_known"] is False and d["command"] == "Std Command 0xBA"


def test_find_returns_the_entry():
    assert cmds.find(0x11)["label"] == "On"
    assert cmds.find(0xBA) is None
