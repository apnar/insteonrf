"""Insteon command number → name tables.

Each table maps ``cmd1`` to a label and, optionally, a ``sub`` table keyed
by ``cmd2``. :func:`lookup` picks the table from the packet flags.
:class:`Command` is the enum code should use for the well-known ``cmd1``
values; the tables stay the place for the descriptive text.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class Command(IntEnum):
    """Well-known ``cmd1`` values."""

    ASSIGN_TO_GROUP = 0x01
    DELETE_FROM_GROUP = 0x02
    PRODUCT_DATA_REQUEST = 0x03
    ENTER_LINK_MODE = 0x09
    ENTER_UNLINK_MODE = 0x0A
    GET_ENGINE_VERSION = 0x0D
    PING = 0x0F
    ID_REQUEST = 0x10
    ON = 0x11
    FAST_ON = 0x12
    OFF = 0x13
    FAST_OFF = 0x14
    BRIGHT_ONE_STEP = 0x15
    DIM_ONE_STEP = 0x16
    START_MANUAL_CHANGE = 0x17
    STOP_MANUAL_CHANGE = 0x18
    STATUS_REQUEST = 0x19
    GET_OPERATING_FLAGS = 0x1F
    SET_OPERATING_FLAGS = 0x20
    INSTANT_CHANGE = 0x21
    MANUALLY_TURNED_OFF = 0x22
    MANUALLY_TURNED_ON = 0x23
    REMOTE_SET_BUTTON_TAP = 0x25
    SET_STATUS = 0x27
    SET_MSB = 0x28
    POKE = 0x29
    PEEK = 0x2B
    # 0x2E/0x2F mean "on/off at rate" as standard messages and
    # "extended set/get" / "read-write ALDB" as extended ones (see EXT_CMDS).
    ON_AT_RATE = 0x2E
    OFF_AT_RATE = 0x2F
    BEEP = 0x30

    @property
    def label(self) -> str:
        """The descriptive name from the standard-command table."""
        entry = STD_CMDS.get(int(self))
        return str(entry["label"]) if entry else self.name.replace("_", " ").title()


#: Commands that are safe to transmit at a device: they only ask questions.
BENIGN_COMMANDS = frozenset(
    {Command.PING, Command.GET_ENGINE_VERSION, Command.STATUS_REQUEST, Command.ID_REQUEST}
)

STD_CMDS: dict[int, dict[str, Any]] = {
    0x01: {"label": "Assign to Group"},
    0x02: {"label": "Delete from Group"},
    0x03: {"label": "Product Data Request",
           "sub": {0x00: "Product Data", 0x01: "FX Username", 0x02: "Device Text String"}},
    0x09: {"label": "Enter Link Mode"},
    0x0A: {"label": "Enter Unlink Mode"},
    0x0D: {"label": "Get Insteon Engine Version"},
    0x0F: {"label": "Ping"},
    0x10: {"label": "ID Request"},
    0x11: {"label": "On"},
    0x12: {"label": "Fast On"},
    0x13: {"label": "Off"},
    0x14: {"label": "Fast Off"},
    0x15: {"label": "Bright One Step"},
    0x16: {"label": "Dim One Step"},
    0x17: {"label": "Start Manual Change", "sub": {0x00: "Dim", 0x01: "Bright"}},
    0x18: {"label": "Stop Manual Change"},
    0x19: {"label": "Status Request"},
    0x1F: {"label": "Get Operating Flags"},
    0x20: {"label": "Set Operating Flags",
           "sub": {0x00: "Program Lock On", 0x01: "Program Lock Off",
                   0x02: "LED On", 0x03: "LED Off",
                   0x04: "Beeper On", 0x05: "Beeper Off",
                   0x06: "Stay Awake On", 0x07: "Stay Awake Off",
                   0x08: "Listen Only On", 0x09: "Listen Only Off",
                   0x0A: "No I'm Alive On", 0x0B: "No I'm Alive Off"}},
    0x21: {"label": "Light Instant Change"},
    0x22: {"label": "Light Manually Turned Off"},
    0x23: {"label": "Light Manually Turned On"},
    0x25: {"label": "Remote Set Button Tap", "sub": {0x01: "1 Tap", 0x02: "2 Taps"}},
    0x27: {"label": "Light Set Status"},
    0x28: {"label": "Set MSB for Peek/Poke"},
    0x29: {"label": "Poke"},
    0x2B: {"label": "Peek"},
    0x2E: {"label": "Light On at Rate"},
    0x2F: {"label": "Light Off at Rate"},
    0x30: {"label": "Beep"},
    0x45: {"label": "Output ON (EZIO)"},
    0x46: {"label": "Output OFF (EZIO)"},
    0x48: {"label": "Write Output Port (EZIO)"},
    0x49: {"label": "Read Output Port (EZIO)"},
    0x4A: {"label": "Get Sensor Value (EZIO)"},
    0x4B: {"label": "Set Sensor 1 OFF->ON Alarm"},
    0x4C: {"label": "Set Sensor 1 ON->OFF Alarm"},
    0x4D: {"label": "Write Configuration Port (EZIO)"},
    0x4E: {"label": "Read Configuration Port (EZIO)"},
    0x4F: {"label": "EZIO Control"},
    0x80: {"label": "Reserved"},
    0x81: {"label": "Assign to Companion Group"},
}

EXT_CMDS: dict[int, dict[str, Any]] = {
    0x03: {"label": "Data Response",
           "sub": {0x00: "Product Data", 0x01: "FX Username", 0x02: "Device Text String",
                   0x03: "Set Device Text String", 0x04: "Set ALL-Link Command Alias",
                   0x05: "Set ALL-Link Command Alias Extended Data"}},
    0x2A: {"label": "Block Data Transfer",
           "sub": {0x00: "Transfer Failure", **{i: "Transfer Complete" for i in range(1, 13)},
                   0x0D: "Transfer Continues", 0xFF: "Request Block Data Transfer"}},
    0x2E: {"label": "Extended Set/Get"},
    0x2F: {"label": "Read/Write ALL-Link Database"},
    0x30: {"label": "Trigger ALL-Link Command"},
    0x4B: {"label": "I/O Set Sensor Normal"},
    0x4C: {"label": "I/O Alarm Data Response"},
    **{c: {"label": "FX User-specific Command"} for c in range(0xF0, 0x100)},
}

BCAST_STD_CMDS: dict[int, dict[str, Any]] = {
    0x01: {"label": "Set Button Pressed (Responder)"},
    0x02: {"label": "Set Button Pressed (Controller)"},
    0x03: {"label": "Test Powerline Phase", "sub": {0x00: "Phase A", 0x01: "Phase B"}},
    # [UNVERIFIED] inherited from upstream. In 5 months of PLM logs 0x04 appears
    # only as a *direct* message, never as a broadcast, and battery devices send
    # their heartbeat as 0x11/0x13 on group 4 instead. Kept in case some device
    # does use it, but do not trust the name.
    0x04: {"label": "Heartbeat"},
    # Broadcast by a controller when an ALL-Link command finishes. cmd2 is the
    # number of responders that did not answer the cleanup — verified against
    # 11,184 of these in a real PLM log, where it matched insteon-mqtt's own
    # success/"had N fails" verdict in every case. This is the third most
    # common message on a busy network, after On and Off.
    0x06: {"label": "ALL-Link Cleanup Status Report",
           "cmd2": lambda c2: "all responders answered" if c2 == 0
                   else f"{c2} responder{'s' if c2 != 1 else ''} did not answer"},
    0x11: {"label": "On"},
    0x12: {"label": "Fast On"},
    0x13: {"label": "Off"},
    0x14: {"label": "Fast Off"},
    0x15: {"label": "Bright One Step"},
    0x16: {"label": "Dim One Step"},
    0x17: {"label": "Start Manual Change", "sub": {0x00: "Dim", 0x01: "Bright"}},
    0x18: {"label": "Stop Manual Change"},
    0x27: {"label": "Device Status Changed"},
    0x49: {"label": "SALad Debug Report"},
}

BCAST_EXT_CMDS: dict[int, dict[str, Any]] = {}

#: Which table to consult first for each ``(extended, broadcast)`` combination,
#: and what to fall back to. The broadcast tables only need entries where a
#: command *means something different* when broadcast; everything else shares
#: the standard meaning, so falling through beats duplicating the tables (and
#: beats reporting "Bcast Command 0x09" for a plain Enter Link Mode).
_TABLES: dict[tuple[bool, bool], tuple[str, tuple[dict[int, dict[str, Any]], ...]]] = {
    (False, False): ("Std", (STD_CMDS,)),
    (True, False): ("Ext", (EXT_CMDS,)),
    (False, True): ("Bcast", (BCAST_STD_CMDS, STD_CMDS)),
    (True, True): ("Bcast Ext", (BCAST_EXT_CMDS, EXT_CMDS, STD_CMDS)),
}


def find(cmd1: int, *, extended: bool = False, bcast: bool = False) -> dict[str, Any] | None:
    """The table entry for ``cmd1``, following the fallback chain, or None."""
    _kind, tables = _TABLES[(bool(extended), bool(bcast))]
    for table in tables:
        entry = table.get(cmd1)
        if entry is not None:
            return entry
    return None


def is_known(cmd1: int, *, extended: bool = False, bcast: bool = False) -> bool:
    """Whether this command has a name, i.e. :func:`lookup` will not fall back.

    ``insteon-rf monitor --unknown-commands`` uses this to surface commands the
    tables do not cover instead of letting them hide in the log as
    ``Std Command 0x??``.
    """
    return find(cmd1, extended=extended, bcast=bcast) is not None


def ambiguous(cmd1: int) -> bool:
    """True when ``cmd1`` means different things standard vs extended.

    An ACK is always a standard message but echoes the query's ``cmd1``, so for
    these numbers a reply cannot be named from one packet alone — see
    :mod:`insteonrf.context`.
    """
    std, ext = STD_CMDS.get(cmd1), EXT_CMDS.get(cmd1)
    return bool(std and ext and std["label"] != ext["label"])


def both_labels(cmd1: int) -> str:
    """``"standard / extended"`` for an ambiguous command, for honest output."""
    return f"{STD_CMDS[cmd1]['label']} / {EXT_CMDS[cmd1]['label']}"


def lookup(cmd1: int, cmd2: int | None = None, *, extended: bool = False,
           bcast: bool = False, ack: bool = False) -> str:
    """Return a human-readable name for ``cmd1``.

    ``cmd2`` refines the answer where it selects a sub-command — but only when
    ``ack`` is false. In an ACK or NAK, ``cmd2`` is the device's *reply*: an
    on-level, an engine version, a peeked byte. Reading it as a sub-command
    there produces confident nonsense, such as reporting the ACK of Get
    Operating Flags as "Set Operating Flags: LED On".
    """
    kind, _tables = _TABLES[(bool(extended), bool(bcast))]
    entry = find(cmd1, extended=extended, bcast=bcast)
    if entry is None:
        return f"{kind} Command 0x{cmd1:02X}"
    label = str(entry["label"])
    if cmd2 is None or ack:
        return label
    sub = entry.get("sub")
    if sub and cmd2 in sub:
        return f"{label}: {sub[cmd2]}"
    fmt = entry.get("cmd2")
    if fmt is not None:
        return f"{label}: {fmt(cmd2)}"
    return label
