#!/usr/bin/env python3
"""Score one receiver against a fixed reference set, from the mesh's event log.

Every fused event in ``insteon-rf-mesh.jsonl`` lists who heard it
(``heard_by``). For a time window this counts the events the *reference*
receivers decoded (CRC good on at least one of them) and asks how many of
those the *target* also heard, and decoded. The reference is fixed on
purpose: "% of all fused events" moves whenever any receiver drops out
(CLAUDE.md), so an A/B between two settings of one board is only fair
against receivers whose own settings did not change.

Device-sent and PLM-sent messages are reported separately, and for the PLM's
also how often the target heard the *original* transmission rather than only
a hop repeat ("1st"): the original arrives out of silence, and the CC11xx
radios lose it far more often than the repeat 50 ms later. The PLM sits
beside the receivers and every board hears it; the distant devices' replies
are what a better setting can actually win.

Usage::

    python tools/score_receivers.py --since 13:05 --until 13:10
    python tools/score_receivers.py --windows windows.txt   # "label start end" per line
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics
import sys

LOG = pathlib.Path("/nvme/churn/insteonrf-mesh/log/insteon-rf-mesh.jsonl")
PLM = "2B.93.07"


def parse_time(s: str) -> float:
    """``HH:MM[:SS]`` today, a full ISO time, or epoch seconds."""
    try:
        return float(s)
    except ValueError:
        pass
    if "T" in s or "-" in s:
        return dt.datetime.fromisoformat(s).timestamp()
    t = dt.time.fromisoformat(s)
    return dt.datetime.combine(dt.date.today(), t).timestamp()


def load(paths: list[pathlib.Path], start: float, end: float) -> list[dict]:
    out = []
    for p in paths:
        if not p.exists():
            continue
        with p.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if start <= ev.get("timestamp", 0) < end:
                    out.append(ev)
    return out


def device_sent(ev: dict) -> bool:
    src = ev.get("from")
    if src:
        return src.upper() != PLM
    # An ACK too damaged to name its sender is still addressed to the PLM.
    return (ev.get("to") or "").upper() == PLM


def quiet_before(events: list[dict]) -> list[float]:
    """Seconds of air with no fused event before each event (events sorted).

    Measured from the previous event's last copy, so a hop repeat or an ACK
    does not count as the start of a new burst.
    """
    out, last = [], None
    for ev in events:
        t = ev.get("first_seen", ev.get("timestamp", 0.0))
        out.append(t - last if last is not None else float("inf"))
        end = ev.get("last_seen", t)
        last = end if last is None else max(last, end)
    return out


def score(events: list[dict], ref: list[str], target: str, after_silence: float = 0.0,
          busy_gap: float = 0.0) -> dict:
    rows: dict[str, dict] = {}
    for kind in ("device", "plm", "quiet", "busy", "all"):
        rows[kind] = {"ref": 0, "heard": 0, "decoded": 0, "rssi": [], "first": 0}
    events = sorted(events, key=lambda e: e.get("first_seen", e.get("timestamp", 0.0)))
    gaps = quiet_before(events)
    for ev, gap in zip(events, gaps, strict=True):
        hb = ev.get("heard_by") or {}
        if not any(hb.get(r, {}).get("crc_ok") for r in ref):
            continue
        kind = "device" if device_sent(ev) else "plm"
        kinds = [kind, "all"]
        # PLM messages that arrive out of at least `after_silence` seconds of
        # quiet: the case the CC11xx radios lose most.
        if kind == "plm" and after_silence and gap >= after_silence:
            kinds.append("quiet")
        # ...and those that follow other traffic closely, where a capture
        # still running from the previous exchange can be in the way.
        if kind == "plm" and busy_gap and gap < busy_gap:
            kinds.append("busy")
        for k in kinds:
            row = rows[k]
            row["ref"] += 1
            t = hb.get(target)
            if t:
                row["heard"] += 1
                # Heard the original transmission, not only a hop repeat:
                # the copy that arrives out of silence, on a receiver whose
                # AGC has had no burst to settle on.
                if t.get("hops_left") is not None and t.get("hops_left") == ev.get("max_hops"):
                    row["first"] += 1
                if t.get("crc_ok"):
                    row["decoded"] += 1
                if t.get("rssi_dbm") is not None:
                    row["rssi"].append(t["rssi_dbm"])
    return rows


def fmt(label: str, rows: dict) -> str:
    parts = [f"{label:24}"]
    for kind in ("device", "plm", "quiet", "busy", "all"):
        r = rows[kind]
        if kind in ("quiet", "busy") and not r["ref"]:
            continue
        n = r["ref"] or 1
        med = f"{statistics.median(r['rssi']):.0f}" if r["rssi"] else "--"
        first = f", 1st {100 * r['first'] / n:5.1f}%" if kind in ("plm", "quiet", "busy") else ""
        parts.append(f"{kind}: {r['decoded']:4}/{r['ref']:<4} {100 * r['decoded'] / n:5.1f}% "
                     f"(heard {100 * r['heard'] / n:5.1f}%{first}, rssi {med})")
    return "  ".join(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--log", type=pathlib.Path, default=LOG)
    p.add_argument("--ref", default="dongle,v3", help="comma-separated reference receivers")
    p.add_argument("--target", default="insteon-rf-embed",
                   help="receiver to score; comma-separated scores several in turn")
    p.add_argument("--after-silence", type=float, default=0.0, metavar="SECONDS",
                   help="also report PLM messages preceded by at least this much quiet "
                        "('quiet' column): out-of-silence bursts are the CC11xx weak spot")
    p.add_argument("--busy-gap", type=float, default=0.0, metavar="SECONDS",
                   help="also report PLM messages that follow other traffic within this "
                        "many seconds ('busy' column)")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--windows", type=pathlib.Path, help='file of "label start end" lines')
    a = p.parse_args(argv)
    ref = [r for r in a.ref.split(",") if r]
    paths = [a.log.with_name(a.log.name + ".1"), a.log]

    windows: list[tuple[str, float, float]] = []
    if a.windows:
        for line in a.windows.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                label, s, e = line.split()[:3]
                windows.append((label, parse_time(s), parse_time(e)))
    elif a.since:
        windows.append(("window", parse_time(a.since),
                        parse_time(a.until) if a.until else dt.datetime.now().timestamp()))
    else:
        p.error("give --since/--until or --windows")

    for label, s, e in windows:
        events = load(paths, s, e)
        for target in a.target.split(","):
            name = label if "," not in a.target else f"{label}:{target[:8]}"
            print(fmt(name, score(events, ref, target, a.after_silence, a.busy_gap)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
