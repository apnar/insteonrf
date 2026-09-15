"""Decide whether a fused RF event should be handed to insteon-mqtt.

This is the only place in the project that can cause the Insteon protocol
stack to act on something the PLM did not hear, so it is built to say no.

Every check is a refusal, in order of how badly it would go wrong:

1. **The kill switch and shadow mode.** Shadow is the default: decide, log,
   publish nothing. Every rollout stage runs shadowed first.
2. **Replies are never injected.** An ACK or NAK is an answer to a command
   insteon-mqtt is actively waiting on, with its own handler state machine and
   timeouts. Feeding it a second answer — or an answer to a command it already
   gave up on — is a different and much larger risk than telling it about a
   broadcast it missed. Off unless deliberately enabled, and there is no tier
   that enables it today.
3. **The PLM's own transmissions are never injected.** The listeners hear the
   modem too. Handing the modem back its own outbound message would be
   nonsense, and it is easy to do by accident.
4. **Anything the PLM already heard is suppressed.** insteon-mqtt has its own
   duplicate check, but its window is ``hops_left * 0.087`` seconds — which is
   *zero* for a copy that arrives with no hops left. Measured on live air: two
   copies of one ACK at hops 1 and 0 were both processed, 87 ms apart. So
   suppression cannot be delegated upstream; this keeps its own fixed window.
5. **Unverified bytes are never injected.** CRC must pass and the frame-index
   counters must not have been rejected. A combined packet (recovered by
   voting across receivers rather than heard intact by anyone) additionally
   needs enough receivers to have voted, because a two-way vote ties on every
   disagreement and leaves the CRC doing all the work.
6. **Rate limits.** Inbound messages push out insteon-mqtt's next allowed
   transmit (``set_wait_time``), so a flood of injections would stall outbound
   commands. Per-device and global caps, both deliberately low.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .fusion import FusedEvent
from .packet import Address, MsgType, Packet
from .plm import PlmFormatError, from_plm_bytes, message_key, to_plm_bytes

log = logging.getLogger(__name__)

#: How long a message the PLM reported is remembered. Fixed, unlike
#: insteon-mqtt's hop-dependent window (see the module docstring).
SUPPRESS_WINDOW_S = 2.0
#: Smallest gap between two injections naming the same sender.
PER_DEVICE_INTERVAL_S = 30.0
#: Ceiling on injections per minute across all devices.
GLOBAL_PER_MINUTE = 6
#: A combined packet needs at least this many receivers to have contributed.
MIN_COMBINED_RECEIVERS = 3

#: Replies. Never injected: they interact with handler state and timeouts.
REPLY_TYPES = frozenset(
    {
        MsgType.DIRECT_ACK,
        MsgType.DIRECT_NAK,
        MsgType.GROUP_CLEANUP_ACK,
        MsgType.GROUP_CLEANUP_NAK,
    }
)


class PlmMemory:
    """What the modem recently reported hearing.

    Deliberately separate from :class:`Injector` so the comparison can run
    with injection switched off — measuring the miss rate is the whole point
    of the first phase, and it must not require enabling the thing it is
    meant to justify.

    The window is fixed, unlike insteon-mqtt's ``hops_left * 0.087`` seconds,
    which is *zero* for a copy arriving with no hops left.
    """

    def __init__(self, window_s: float = SUPPRESS_WINDOW_S):
        self.window_s = window_s
        self.frames = 0
        self._seen: dict[tuple[Any, ...], float] = {}

    def note(self, raw: bytes | bytearray | memoryview, now: float | None = None) -> bool:
        """Record an inbound frame. Non-inbound codes (the ``0x62`` echo of
        the modem's own transmissions, modem info replies) are ignored."""
        try:
            pkt = from_plm_bytes(raw)
        except PlmFormatError:
            return False
        self._seen[message_key(pkt)] = time.time() if now is None else now
        self.frames += 1
        return True

    def heard(self, key: tuple[Any, ...], now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        when = self._seen.get(key)
        return when is not None and now - when <= self.window_s

    def forget_old(self, now: float) -> None:
        for key, when in list(self._seen.items()):
            if now - when > max(self.window_s, 10.0):
                del self._seen[key]


class Tier(IntEnum):
    """Categories of message, enabled one at a time as confidence grows."""

    #: Broadcasts from RF-only battery devices. Safest and highest value:
    #: they cannot be polled afterwards, so injection is the only mechanism
    #: that can help them, and they are never party to a command exchange.
    BATTERY = 1
    #: Group broadcasts and ALL-Link cleanup reports from mains devices.
    GROUP = 2
    #: Unsolicited direct messages from a device to the modem.
    STATE = 3


@dataclass
class Decision:
    """Why an event was or was not injected."""

    inject: bool
    reason: str
    tier: Tier | None = None
    frame: bytes | None = None
    #: True when only shadow mode stopped it.
    would_inject: bool = False

    def __bool__(self) -> bool:
        return self.inject


@dataclass
class Counters:
    considered: int = 0
    injected: int = 0
    would_inject: int = 0
    refused: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        self.refused[reason] = self.refused.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "injected": self.injected,
            "would_inject": self.would_inject,
            "refused": dict(sorted(self.refused.items(), key=lambda kv: -kv[1])),
        }


def sender_of(packet: Packet) -> Address | None:
    """Who transmitted this, whichever address slot it sits in.

    A group broadcast puts the sender in the "to" slot; everything else puts
    it in "from". :attr:`Packet.from_addr` deliberately returns ``None`` for a
    group broadcast, so asking for the sender needs both cases.
    """
    return packet.to_addr if packet.is_group_broadcast else packet.from_addr


class Injector:
    """Turn fused RF events into injections, cautiously."""

    def __init__(
        self,
        publish: Callable[[bytes, FusedEvent], None] | None = None,
        *,
        plm_addr: Address | str | None = None,
        battery_addrs: Iterable[Address | str] = (),
        allow: Iterable[Tier] = (),
        shadow: bool = True,
        enabled: Callable[[], bool] = lambda: True,
        suppress_window_s: float = SUPPRESS_WINDOW_S,
        per_device_interval_s: float = PER_DEVICE_INTERVAL_S,
        global_per_minute: int = GLOBAL_PER_MINUTE,
        min_combined_receivers: int = MIN_COMBINED_RECEIVERS,
        memory: PlmMemory | None = None,
    ):
        self.publish = publish
        self.plm_addr = Address(plm_addr) if plm_addr is not None else None
        self.battery_addrs = {Address(a) for a in battery_addrs}
        self.allow = frozenset(allow)
        self.shadow = shadow
        self.enabled = enabled
        self.suppress_window_s = suppress_window_s
        self.per_device_interval_s = per_device_interval_s
        self.global_per_minute = global_per_minute
        self.min_combined_receivers = min_combined_receivers

        self.counters = Counters()
        #: Shared with the service when there is one, so suppression and the
        #: miss table always agree on what the modem heard.
        self.memory = memory if memory is not None else PlmMemory(suppress_window_s)
        self._last_per_device: dict[Address, float] = {}
        self._recent_injections: list[float] = []

    # -- what the PLM heard ----------------------------------------------
    def note_plm(self, raw: bytes | bytearray | memoryview, now: float | None = None) -> bool:
        """Record a frame the modem reported on ``insteon/raw/rx``.

        Non-inbound codes (the ``0x62`` echo of the modem's own transmissions,
        modem info replies and so on) are not messages we could ever inject,
        so they are ignored rather than keyed.
        """
        return self.memory.note(raw, now)

    def plm_heard(self, key: tuple[Any, ...], now: float) -> bool:
        return self.memory.heard(key, now)

    # -- classification ---------------------------------------------------
    def classify(self, packet: Packet) -> tuple[Tier | None, str]:
        """Which tier this message belongs to, or why it belongs to none."""
        if packet.msg_type in REPLY_TYPES:
            return None, "reply to an outstanding command"

        sender = sender_of(packet)
        if sender is None:
            return None, "no sender address"
        if self.plm_addr is not None and sender == self.plm_addr:
            return None, "the PLM's own transmission"

        if sender in self.battery_addrs:
            return Tier.BATTERY, "battery device"
        if packet.is_group_broadcast or packet.msg_type == MsgType.GROUP_CLEANUP:
            return Tier.GROUP, "group broadcast"
        if packet.msg_type == MsgType.DIRECT:
            return Tier.STATE, "unsolicited direct"
        return None, f"unhandled message type {packet.msg_type.name}"

    # -- the decision -----------------------------------------------------
    def consider(self, event: FusedEvent, now: float | None = None) -> Decision:
        """Decide what to do with one fused event, and do it."""
        if now is None:
            now = time.time()
        self.counters.considered += 1
        self._forget_old(now)

        packet = event.packet

        if not self.enabled():
            return self._no("kill switch")

        tier, why = self.classify(packet)
        if tier is None:
            return self._no(why)
        if tier not in self.allow:
            return self._no(f"tier {tier.name} not enabled")

        if packet.crc_ok is not True:
            return self._no("CRC did not pass")
        if packet.index_ok is False:
            return self._no("frame-index counters rejected")
        if packet.extended and packet.ext_crc_ok is False:
            return self._no("extended data CRC did not pass")
        if event.combined and event.combined_from < self.min_combined_receivers:
            return self._no(
                f"combined from only {event.combined_from} receivers "
                f"(need {self.min_combined_receivers})"
            )

        if self.plm_heard(event.key, now):
            event.plm_saw_it = True
            return self._no("the PLM already heard it")
        event.plm_saw_it = False

        sender = sender_of(packet)
        assert sender is not None  # classify() already refused None
        last = self._last_per_device.get(sender)
        if last is not None and now - last < self.per_device_interval_s:
            return self._no("per-device rate limit")
        if len(self._recent_injections) >= self.global_per_minute:
            return self._no("global rate limit")

        try:
            frame = to_plm_bytes(packet)
        except PlmFormatError as exc:
            return self._no(f"cannot render as a PLM frame: {exc}")

        if self.shadow:
            self.counters.would_inject += 1
            log.info("shadow: would inject %s (%s)", packet.summary(), tier.name)
            return Decision(False, "shadow mode", tier, frame, would_inject=True)

        self._last_per_device[sender] = now
        self._recent_injections.append(now)
        self.counters.injected += 1
        if self.publish is not None:
            self.publish(frame, event)
        log.info("injected %s (%s, closest=%s)", packet.summary(), tier.name, event.closest)
        return Decision(True, "injected", tier, frame)

    # -- internals --------------------------------------------------------
    def _no(self, reason: str) -> Decision:
        self.counters.refuse(reason)
        return Decision(False, reason)

    def _forget_old(self, now: float) -> None:
        self.memory.forget_old(now)
        self._recent_injections = [t for t in self._recent_injections if now - t < 60.0]


def load_battery_addrs(path: str) -> set[Address]:
    """Read one Insteon address per line (``#`` comments allowed).

    Build the list from the insteon-mqtt config, which knows which device
    classes have no powerline path::

        grep -hoE '^- [0-9a-fA-F]{2}\\.[0-9a-fA-F]{2}\\.[0-9a-fA-F]{2}' \\
          /k8s/insteon-config/{remotes,water_sensors,door_sensors}.yaml \\
          | cut -d' ' -f2 > battery.txt
    """
    out = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(Address(line))
    return out


__all__ = [
    "GLOBAL_PER_MINUTE",
    "MIN_COMBINED_RECEIVERS",
    "PER_DEVICE_INTERVAL_S",
    "REPLY_TYPES",
    "SUPPRESS_WINDOW_S",
    "Counters",
    "Decision",
    "Injector",
    "PlmMemory",
    "Tier",
    "load_battery_addrs",
    "sender_of",
]
