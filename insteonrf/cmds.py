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
    0x04: {"label": "Heartbeat"},
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

_TABLE_NAMES = {
    (False, False): ("Std", STD_CMDS),
    (True, False): ("Ext", EXT_CMDS),
    (False, True): ("Bcast", BCAST_STD_CMDS),
    (True, True): ("Bcast Ext", BCAST_EXT_CMDS),
}


def lookup(cmd1: int, cmd2: int | None = None, *, extended: bool = False, bcast: bool = False) -> str:
    """Return a human-readable name for ``cmd1`` (and ``cmd2`` where it selects a sub-command)."""
    kind, table = _TABLE_NAMES[(bool(extended), bool(bcast))]
    entry = table.get(cmd1)
    if entry is None:
        return f"{kind} Command 0x{cmd1:02X}"
    sub = entry.get("sub")
    if sub and cmd2 is not None and cmd2 in sub:
        return f"{entry['label']}: {sub[cmd2]}"
    return str(entry["label"])
