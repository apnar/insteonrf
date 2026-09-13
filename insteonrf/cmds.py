"""Insteon command number → name tables.

Each table maps ``cmd1`` to a label and, optionally, a ``sub`` table keyed
by ``cmd2``. :func:`lookup` picks the table from the packet flags.
"""

from __future__ import annotations

STD_CMDS: dict[int, dict] = {
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

EXT_CMDS: dict[int, dict] = {
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

BCAST_STD_CMDS: dict[int, dict] = {
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

BCAST_EXT_CMDS: dict[int, dict] = {}

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
    return entry["label"]
