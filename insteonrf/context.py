"""Cross-packet context: read an ACK's command in the table its query came from.

An ACK or NAK is always a *standard* message, even when it answers an extended
command — and it echoes the query's ``cmd1``. Several command numbers mean
different things in the two tables, so a single packet is genuinely ambiguous:

===========  =========================  ===============================
``cmd1``     standard                   extended
===========  =========================  ===============================
``0x03``     Product Data Request       Data Response
``0x2E``     Light On at Rate           Extended Set/Get
``0x2F``     Light Off at Rate          Read/Write ALL-Link Database
``0x30``     Beep                       Trigger ALL-Link Command
===========  =========================  ===============================

This is not academic. In five months of one network's logs, 3,712 of the 3,722
standard ``0x2F`` messages arrived while an extended ``0x2F`` to that same
device was outstanding: every one was the ACK of an ALDB read being reported as
"Light Off at Rate". A wrong name is worse than no name.

:class:`CommandTracker` fixes it the only way a receiver can — by remembering
what was asked. Feed it every packet in arrival order and it marks each ACK
with whether its query was extended, which :attr:`Packet.cmd_name` then uses.
"""

from __future__ import annotations

import time

from .packet import Packet

#: How long a query stays eligible to explain an ACK. Insteon answers in
#: milliseconds; seconds of slack costs nothing and covers a retried query.
DEFAULT_WINDOW_S = 5.0


class CommandTracker:
    """Remember recent queries so replies can be read in the right table."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S):
        self.window_s = window_s
        #: ``(sender, target, cmd1) -> (when, was extended)``
        self._queries: dict[tuple[str, str, int], tuple[float, bool]] = {}
        self.resolved = 0
        self.unresolved = 0

    def observe(self, pkt: Packet) -> Packet:
        """Record a query, or annotate a reply. Returns the same packet."""
        now = pkt.timestamp if pkt.timestamp is not None else time.time()
        self._expire(now)
        if pkt.cmd1 is None or pkt.to_addr is None:
            return pkt
        if pkt.ack:
            self._annotate(pkt, now)
        elif pkt.from_addr is not None:
            key = (str(pkt.from_addr), str(pkt.to_addr), pkt.cmd1)
            self._queries[key] = (now, pkt.extended)
        return pkt

    def _annotate(self, pkt: Packet, now: float) -> None:
        # The reply comes back with the addresses swapped.
        if pkt.from_addr is None or pkt.cmd1 is None:
            return
        key = (str(pkt.to_addr), str(pkt.from_addr), pkt.cmd1)
        hit = self._queries.get(key)
        if hit is None:
            self.unresolved += 1
            return
        pkt.ack_of_extended = hit[1]
        self.resolved += 1

    def _expire(self, now: float) -> None:
        for key, (when, _) in list(self._queries.items()):
            if now - when > self.window_s:
                del self._queries[key]

    def __len__(self) -> int:
        return len(self._queries)


def annotate(packets: list[Packet], tracker: CommandTracker | None = None) -> list[Packet]:
    """Run a batch of packets through a tracker in order (convenience)."""
    tracker = tracker or CommandTracker()
    for pkt in packets:
        tracker.observe(pkt)
    return packets
