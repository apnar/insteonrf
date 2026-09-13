"""Catching phantom "all on" events, and pinning them on a device.

Early Insteon devices (i1 and i2, roughly pre-2012) act on an ALL-Link
broadcast addressed to **group 0**, which means "every device". The command was
dropped from later firmware, but legacy devices still obey it — so a single
malformed group-0 broadcast turns a whole house on at once. Standard Insteon
*powerline* messages carry no CRC, so a corrupted byte can survive repetition
and be acted upon.

The RF side is where this is diagnosable, for two reasons:

* **RF packets carry a CRC.** A group-0 On that arrives with a *valid* CRC was
  genuinely transmitted that way by some device — the corruption happened
  before the sender computed the checksum, i.e. inside that device. One with a
  *failed* CRC was mangled in flight and is a different problem.
* **Hop counts locate the origin.** A transmission leaves its sender with
  ``hops_left == max_hops`` and every repeat decrements it. The copy with the
  hop counter still intact is the closest thing to the source, and its RSSI
  says how near the receiver was.

So this watcher keeps a rolling window of *every* burst, and when a suspicious
message appears it writes the whole window out — the trigger, its repeats, and
whatever the mesh did next — plus an attribution report. Point two or three
receivers at the problem and the RSSI ordering tells you where in the house it
came from.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .packet import Packet

#: Commands that turn a load on. A group-0 broadcast of one of these is the
#: classic phantom all-on; Fast On behaves the same way but ignores ramp rates.
ON_COMMANDS = (0x11, 0x12)

#: The all-devices group. Nothing legitimate on a modern network addresses it.
ALL_DEVICES_GROUP = 0

#: How much history to keep for context around a trigger.
DEFAULT_WINDOW_S = 30.0

#: A storm is this many distinct group broadcasts inside `storm_window_s`.
DEFAULT_STORM_COUNT = 12
DEFAULT_STORM_WINDOW_S = 3.0


@dataclass
class Seen:
    """One burst as it arrived, kept for context."""

    at: float
    bits: str = field(repr=False, default="")
    packet: Packet | None = None
    rssi_dbm: float | None = None
    snr_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"at": self.at, "rssi_dbm": self.rssi_dbm, "snr_db": self.snr_db}
        d["packet"] = self.packet.to_dict() if self.packet is not None else None
        d["bits"] = self.bits
        return d


@dataclass
class Trigger:
    """Something worth waking a human for."""

    kind: str
    at: float
    detail: str
    packet: Packet | None = None

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


@dataclass
class Suspect:
    """Running tally for one sender, so a repeat offender stands out."""

    address: str
    packets: int = 0
    crc_failures: int = 0
    group0: int = 0
    unknown_group: int = 0
    repaired: int = 0
    rssi_sum: float = 0.0
    rssi_count: int = 0

    @property
    def mean_rssi(self) -> float | None:
        return self.rssi_sum / self.rssi_count if self.rssi_count else None

    @property
    def score(self) -> int:
        """Crude suspicion ranking: malformed all-link traffic counts most."""
        return self.group0 * 100 + self.unknown_group * 10 + self.crc_failures

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "packets": self.packets,
                "crc_failures": self.crc_failures, "group0": self.group0,
                "unknown_group": self.unknown_group, "repaired": self.repaired,
                "mean_rssi_dbm": round(self.mean_rssi, 1) if self.mean_rssi else None,
                "score": self.score}


class AllOnWatcher:
    """Watch a packet stream for phantom-all-on triggers and keep the evidence."""

    def __init__(self, *, known_groups: Iterable[int] | None = None,
                 window_s: float = DEFAULT_WINDOW_S,
                 storm_count: int = DEFAULT_STORM_COUNT,
                 storm_window_s: float = DEFAULT_STORM_WINDOW_S,
                 dump_dir: str | Path | None = None):
        #: Groups the network legitimately uses. Without this, only group 0 is
        #: flagged — a novel group number is only suspicious if you know which
        #: ones are real (generate the list from your scene definitions).
        self.known_groups = set(known_groups) if known_groups is not None else None
        self.window_s = window_s
        self.storm_count = storm_count
        self.storm_window_s = storm_window_s
        self.dump_dir = Path(dump_dir) if dump_dir else None
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
        self.history: list[Seen] = []
        self.suspects: dict[str, Suspect] = {}
        self.triggers: list[Trigger] = []
        self._recent_groups: list[tuple[float, int]] = []

    # ---- ingest ----

    def observe(self, pkt: Packet | None, *, bits: str = "", rssi_dbm: float | None = None,
                snr_db: float | None = None, at: float | None = None) -> list[Trigger]:
        """Record a burst and return any triggers it raised."""
        now = at if at is not None else (pkt.timestamp if pkt and pkt.timestamp else time.time())
        self.history.append(Seen(now, bits, pkt, rssi_dbm, snr_db))
        self._expire(now)
        if pkt is None:
            return []
        self._tally(pkt, rssi_dbm)
        found = self._check(pkt, now)
        for trig in found:
            self.triggers.append(trig)
            if self.dump_dir:
                self.dump(trig)
        return found

    def _tally(self, pkt: Packet, rssi_dbm: float | None) -> None:
        who = str(pkt.from_addr or pkt.to_addr or "unknown")
        s = self.suspects.setdefault(who, Suspect(who))
        s.packets += 1
        if pkt.crc_ok is False:
            s.crc_failures += 1
        if pkt.corrected:
            s.repaired += 1
        if pkt.is_group_broadcast:
            if pkt.group == ALL_DEVICES_GROUP:
                s.group0 += 1
            elif self.known_groups is not None and pkt.group not in self.known_groups:
                s.unknown_group += 1
        if rssi_dbm is not None:
            s.rssi_sum += rssi_dbm
            s.rssi_count += 1

    def _check(self, pkt: Packet, now: float) -> list[Trigger]:
        out: list[Trigger] = []
        if pkt.is_group_broadcast:
            self._recent_groups.append((now, pkt.group if pkt.group is not None else -1))
            self._recent_groups = [(t, g) for t, g in self._recent_groups
                                   if now - t <= self.storm_window_s]
            if pkt.group == ALL_DEVICES_GROUP:
                # The smoking gun. A valid CRC means a device really sent this.
                how = ("CRC valid — some device transmitted this"
                       if pkt.crc_ok else "CRC FAILED — mangled in flight")
                out.append(Trigger(
                    "all-link-group-0", now,
                    f"group 0 ({'ON' if pkt.cmd1 in ON_COMMANDS else f'cmd 0x{pkt.cmd1:02X}'}) "
                    f"from {pkt.from_addr or pkt.to_addr}, hops {pkt.hops_left}/{pkt.max_hops}, "
                    f"{how}", pkt))
            elif (self.known_groups is not None and pkt.group not in self.known_groups
                  and pkt.cmd1 in ON_COMMANDS):
                out.append(Trigger(
                    "unknown-group", now,
                    f"On broadcast to unknown group {pkt.group} from "
                    f"{pkt.to_addr}, hops {pkt.hops_left}/{pkt.max_hops}", pkt))
            if len(self._recent_groups) >= self.storm_count:
                out.append(Trigger(
                    "storm", now,
                    f"{len(self._recent_groups)} group broadcasts in "
                    f"{self.storm_window_s:g}s", pkt))
                self._recent_groups.clear()
        return out

    def _expire(self, now: float) -> None:
        cut = now - self.window_s
        if self.history and self.history[0].at < cut:
            self.history = [h for h in self.history if h.at >= cut]

    # ---- output ----

    def copies_of(self, pkt: Packet) -> list[Seen]:
        """Every buffered copy of this message, ignoring what a repeat changes."""
        def key(p: Packet) -> tuple[int, ...]:
            data = list(p.data[: p.crc_index + 1])
            data[0] &= ~0x0C          # hops-left
            if len(data) > p.crc_index:
                data[p.crc_index] = 0  # and the CRC computed over it
            return tuple(data)

        want = key(pkt)
        return [h for h in self.history if h.packet is not None and key(h.packet) == want]

    def report(self, trig: Trigger) -> str:
        """Human-readable attribution for one trigger."""
        lines = [f"=== {trig.kind} at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(trig.at))}",
                 f"    {trig.detail}"]
        if trig.packet is None:
            return "\n".join(lines)
        lines.append(f"    {trig.packet.summary()}")
        copies = self.copies_of(trig.packet)
        lines.append(f"    {len(copies)} cop{'y' if len(copies) == 1 else 'ies'} of this message "
                     f"in the {self.window_s:g}s window:")
        for c in copies:
            p = c.packet
            assert p is not None
            origin = " <-- hop counter intact: closest to the source" \
                if p.hops_left == p.max_hops else ""
            rssi = f"{c.rssi_dbm:.1f} dBm" if c.rssi_dbm is not None else "rssi n/a"
            lines.append(f"      {time.strftime('%H:%M:%S', time.localtime(c.at))} "
                         f"hops {p.hops_left}/{p.max_hops}  {rssi}  crc_ok={p.crc_ok}{origin}")
        ranked = self.ranked_suspects()[:5]
        if ranked:
            lines.append("    most suspicious senders so far:")
            for s in ranked:
                lines.append(f"      {s.address}  score {s.score}  "
                             f"group0={s.group0} unknown_group={s.unknown_group} "
                             f"crc_fail={s.crc_failures} of {s.packets} packets")
        return "\n".join(lines)

    def ranked_suspects(self) -> list[Suspect]:
        return sorted((s for s in self.suspects.values() if s.score),
                      key=lambda s: -s.score)

    def dump(self, trig: Trigger) -> Path | None:
        """Write the trigger plus the whole context window as JSON."""
        if not self.dump_dir:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(trig.at))
        path = self.dump_dir / f"{trig.kind}-{stamp}.json"
        payload = {
            "trigger": {"kind": trig.kind, "at": trig.at, "detail": trig.detail,
                        "packet": trig.packet.to_dict() if trig.packet else None},
            "report": self.report(trig),
            "window_s": self.window_s,
            "context": [h.to_dict() for h in self.history],
            "suspects": [s.to_dict() for s in self.ranked_suspects()],
        }
        path.write_text(json.dumps(payload, indent=1))
        return path

    def summary(self) -> str:
        """What to print when the watch ends."""
        if not self.triggers and not self.ranked_suspects():
            return "no phantom-all-on triggers, and nothing suspicious"
        lines = [f"{len(self.triggers)} trigger(s):"]
        counts: dict[str, int] = {}
        for t in self.triggers:
            counts[t.kind] = counts.get(t.kind, 0) + 1
        for kind, n in sorted(counts.items()):
            lines.append(f"    {kind}: {n}")
        ranked = self.ranked_suspects()
        if ranked:
            lines.append("suspicious senders (worst first):")
            for s in ranked[:10]:
                rssi = f"{s.mean_rssi:.1f} dBm" if s.mean_rssi is not None else "n/a"
                lines.append(f"    {s.address}  score {s.score}  group0={s.group0} "
                             f"unknown_group={s.unknown_group} crc_fail={s.crc_failures} "
                             f"repaired={s.repaired} of {s.packets} packets, mean rssi {rssi}")
        return "\n".join(lines)


def load_known_groups(path: str | Path) -> set[int]:
    """Read legitimate group numbers from a file.

    Accepts one number per line (``#`` comments allowed) or a JSON array — so a
    list can be generated straight from a scene configuration, e.g.::

        grep -oE '^  - modem: [0-9]+' scenes.yaml | awk '{print $3}' > groups.txt
    """
    text = Path(path).read_text().strip()
    if text.startswith("["):
        return {int(x) for x in json.loads(text)}
    out: set[int] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(int(line))
    return out


def scan(packets: Sequence[Packet], **kwargs: Any) -> AllOnWatcher:
    """Run a batch of packets past a watcher (for offline analysis and tests)."""
    w = AllOnWatcher(**kwargs)
    for pkt in packets:
        w.observe(pkt)
    return w
