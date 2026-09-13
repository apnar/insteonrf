"""Reading an ACK's command in the table its query came from.

An ACK is always a standard message but echoes the query's cmd1, so 0x2F, 0x2E,
0x30 and 0x03 are ambiguous from one packet alone. This was not hypothetical: in
five months of one network's logs, 3,712 of 3,722 standard 0x2F messages were
ACKs of extended ALDB reads being reported as "Light Off at Rate".
"""

import pytest

from insteonrf import cmds
from insteonrf.context import CommandTracker, annotate
from insteonrf.packet import Packet

PLM = "2B.93.07"
DEV = "2C.9B.D9"


def query(cmd1, *, extended=False, ts=100.0, src=PLM, dst=DEV, cmd2=0):
    p = Packet.build(src, dst, cmd1=cmd1, cmd2=cmd2,
                     ext_data=[0] * 13 if extended else None)
    p.timestamp = ts
    return p


def reply(cmd1, *, ts=100.3, src=DEV, dst=PLM, cmd2=0):
    p = Packet.build(src, dst, cmd1=cmd1, cmd2=cmd2, ack=True)
    p.timestamp = ts
    return p


# ---- which commands are ambiguous ----


def test_ambiguous_commands():
    for cmd1 in (0x03, 0x2E, 0x2F, 0x30):
        assert cmds.ambiguous(cmd1), f"0x{cmd1:02X} differs between the tables"
        assert "/" in cmds.both_labels(cmd1)
    for cmd1 in (0x11, 0x13, 0x0D, 0x19):
        assert not cmds.ambiguous(cmd1)


def test_ambiguous_ack_without_context_reports_both():
    """Better two honest names than one confident wrong one."""
    assert reply(0x2F).cmd_name == "Light Off at Rate / Read/Write ALL-Link Database"
    assert reply(0x30).cmd_name == "Beep / Trigger ALL-Link Command"
    assert reply(0x11, cmd2=0xFF).cmd_name == "On"   # unambiguous, unaffected


# ---- correlation ----


@pytest.mark.parametrize("cmd1,extended,expected", [
    (0x2F, True, "Read/Write ALL-Link Database"),
    (0x2F, False, "Light Off at Rate"),
    (0x30, True, "Trigger ALL-Link Command"),
    (0x30, False, "Beep"),
    (0x2E, True, "Extended Set/Get"),
])
def test_tracker_reads_the_reply_in_the_querys_table(cmd1, extended, expected):
    t = CommandTracker()
    t.observe(query(cmd1, extended=extended))
    ack = t.observe(reply(cmd1))
    assert ack.ack_of_extended is extended
    assert ack.cmd_name == expected
    assert t.resolved == 1 and t.unresolved == 0


def test_the_live_captured_case():
    """Exactly what came off the air: an extended 0x30, then its standard ACK."""
    q, a = query(0x30, extended=True), reply(0x30)
    annotate([q, a])
    assert q.cmd_name == "Trigger ALL-Link Command"
    assert a.cmd_name == "Trigger ALL-Link Command"   # was "Beep"


def test_unmatched_ack_is_left_alone():
    t = CommandTracker()
    ack = t.observe(reply(0x2F))
    assert ack.ack_of_extended is None and t.unresolved == 1
    assert "/" in (ack.cmd_name or "")


def test_query_expires():
    t = CommandTracker(window_s=1.0)
    t.observe(query(0x2F, extended=True, ts=100.0))
    ack = t.observe(reply(0x2F, ts=200.0))
    assert ack.ack_of_extended is None and len(t) == 0


def test_addresses_must_match():
    t = CommandTracker()
    t.observe(query(0x2F, extended=True))
    other = t.observe(reply(0x2F, src="29.4E.52"))
    assert other.ack_of_extended is None


def test_group_broadcasts_do_not_confuse_the_tracker():
    t = CommandTracker()
    bcast = Packet.build(PLM, group=68, cmd1=0x11, bcast=True)
    bcast.timestamp = 100.0
    t.observe(bcast)
    assert t.observe(reply(0x2F)).ack_of_extended is None


def test_tracker_used_by_the_decode_path():
    """cli._decode feeds the tracker, so a piped stream gets correlated."""
    from insteonrf import cli

    t = CommandTracker()
    q = query(0x2F, extended=True)
    out = cli._decode(q.to_bits(), 100.0, tracker=t)
    assert out and out[0].cmd_name == "Read/Write ALL-Link Database"
    out = cli._decode(reply(0x2F).to_bits(), 100.3, tracker=t)
    assert out and out[0].cmd_name == "Read/Write ALL-Link Database"
