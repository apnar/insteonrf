"""Catching phantom all-on events.

Legacy Insteon devices act on an ALL-Link broadcast to group 0 ("every
device"), so one malformed group-0 message turns a house on at once. Measured
on a real installation: across three such events, the PLM logged *no trigger
at all* — a PLM only reports group broadcasts it holds an ALDB link for, so an
unlinked group-0 broadcast never reaches the host. That is why the hub shows
the lights as off, and why an unfiltered RF receiver is the only witness.
"""

import json

import pytest

from insteonrf.allon import (
    ALL_DEVICES_GROUP,
    AllOnWatcher,
    load_known_groups,
    scan,
)
from insteonrf.packet import Packet

KPL = "29.41.B5"
PLM = "2B.93.07"


def bcast(src, group, cmd1=0x11, *, hops_left=3, max_hops=3, at=1000.0, crc_bad=False):
    p = Packet.build(src, group=group, cmd1=cmd1, bcast=True,
                     hops_left=hops_left, max_hops=max_hops)
    if crc_bad:
        p.data[p.crc_index] ^= 0xFF
    p.timestamp = at
    return p


# ---- the trigger ----


def test_group_zero_broadcast_is_the_trigger():
    w = scan([bcast(PLM, 68), bcast(KPL, ALL_DEVICES_GROUP)])
    assert len(w.triggers) == 1
    t = w.triggers[0]
    assert t.kind == "all-link-group-0" and "ON" in t.detail
    assert "CRC valid" in t.detail          # so a device really sent it
    assert w.suspects[KPL].group0 == 1


def test_a_failed_crc_says_mangled_in_flight_instead():
    """A valid CRC means the sender transmitted it; a bad one means the air did."""
    w = scan([bcast(KPL, ALL_DEVICES_GROUP, crc_bad=True)])
    assert len(w.triggers) == 1 and "CRC FAILED" in w.triggers[0].detail


def test_ordinary_scene_traffic_raises_nothing():
    pkts = [bcast(PLM, g, at=1000.0 + i) for i, g in enumerate((68, 21, 25, 115, 47))]
    w = scan(pkts)
    assert w.triggers == [] and w.ranked_suspects() == []
    assert "no phantom-all-on triggers" in w.summary()


def test_group_zero_with_any_command_still_flags():
    w = scan([bcast(KPL, ALL_DEVICES_GROUP, cmd1=0x13)])
    assert len(w.triggers) == 1 and "cmd 0x13" in w.triggers[0].detail


# ---- unknown groups ----


def test_unknown_group_needs_a_known_list():
    pkts = [bcast(PLM, 200)]
    assert scan(pkts).triggers == []                      # no list: not suspicious
    w = scan(pkts, known_groups={68, 21, 25})
    assert len(w.triggers) == 1 and w.triggers[0].kind == "unknown-group"
    assert w.suspects[PLM].unknown_group == 1


def test_known_group_is_quiet():
    w = scan([bcast(PLM, 68)], known_groups={68})
    assert w.triggers == []


def test_load_known_groups(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("# scene groups\n68\n21\n\n115  # everything\n")
    assert load_known_groups(f) == {68, 21, 115}
    j = tmp_path / "g.json"
    j.write_text("[1, 2, 3]")
    assert load_known_groups(j) == {1, 2, 3}


# ---- storms ----


def test_storm_detection():
    pkts = [bcast(PLM, 21 + i, at=1000.0 + i * 0.1) for i in range(14)]
    w = scan(pkts, storm_count=12, storm_window_s=3.0)
    assert any(t.kind == "storm" for t in w.triggers)


def test_slow_traffic_is_not_a_storm():
    pkts = [bcast(PLM, 21 + i, at=1000.0 + i * 10) for i in range(14)]
    assert not any(t.kind == "storm" for t in scan(pkts).triggers)


# ---- attribution ----


def test_hop_counter_identifies_the_closest_copy():
    """A transmission leaves its sender with hops_left == max_hops."""
    w = AllOnWatcher()
    for hl in (3, 2, 1):
        w.observe(bcast(KPL, ALL_DEVICES_GROUP, hops_left=hl, at=1000.0 + (3 - hl) * 0.05),
                  rssi_dbm=-60.0 - hl)
    trig = w.triggers[0]
    report = w.report(trig)
    assert "3 copies of this message" in report
    assert "hop counter intact" in report
    assert "hops 3/3" in report


def test_report_ranks_suspects():
    w = AllOnWatcher(known_groups={68})
    w.observe(bcast(KPL, ALL_DEVICES_GROUP))
    w.observe(bcast("25.0C.46", 200, at=1001.0))
    ranked = w.ranked_suspects()
    assert ranked[0].address == KPL                 # group 0 outranks unknown group
    assert ranked[0].score > ranked[1].score
    assert "most suspicious senders" in w.report(w.triggers[0])


def test_context_window_expires():
    w = AllOnWatcher(window_s=5.0)
    w.observe(bcast(PLM, 68, at=1000.0))
    w.observe(bcast(PLM, 68, at=1100.0))
    assert len(w.history) == 1


def test_dump_writes_the_context(tmp_path):
    w = AllOnWatcher(dump_dir=tmp_path)
    w.observe(bcast(PLM, 68, at=999.0), bits="0101", rssi_dbm=-70.0)
    w.observe(bcast(KPL, ALL_DEVICES_GROUP, at=1000.0), bits="1010", rssi_dbm=-55.0)
    files = list(tmp_path.glob("all-link-group-0-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["trigger"]["kind"] == "all-link-group-0"
    assert len(payload["context"]) == 2                    # the trigger and what preceded it
    assert payload["context"][0]["rssi_dbm"] == -70.0
    assert payload["suspects"][0]["address"] == KPL
    assert "hop counter intact" in payload["report"]


def test_crc_failures_and_repairs_count_toward_suspicion():
    w = AllOnWatcher()
    bad = bcast("24.81.1A", 68, crc_bad=True)
    w.observe(bad)
    good = bcast("24.81.1A", 68, at=1001.0)
    good.corrected = 2
    w.observe(good)
    s = w.suspects["24.81.1A"]
    assert s.crc_failures == 1 and s.repaired == 1 and s.packets == 2
    assert s.score >= 1


def test_rssi_is_averaged_per_sender():
    w = AllOnWatcher()
    for i, r in enumerate((-60.0, -70.0)):
        w.observe(bcast(KPL, ALL_DEVICES_GROUP, at=1000.0 + i), rssi_dbm=r)
    assert w.suspects[KPL].mean_rssi == pytest.approx(-65.0)


def test_watcher_tolerates_undecodable_bursts():
    w = AllOnWatcher()
    assert w.observe(None, bits="0101") == []
    assert len(w.history) == 1


def test_summary_lists_triggers_and_suspects():
    w = scan([bcast(KPL, ALL_DEVICES_GROUP)])
    out = w.summary()
    assert "all-link-group-0: 1" in out and KPL in out


def test_cli_allon_runs_over_a_capture(tmp_path, monkeypatch):
    """The preset wires the watcher into monitor."""
    import pathlib

    from insteonrf import cli

    data = pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt"
    monkeypatch.chdir(tmp_path)
    rc = cli.allon_main(["--backend", "file", "--replay", str(data), "--quiet"])
    assert rc == 0
    assert (tmp_path / "allon.jsonl").exists()
