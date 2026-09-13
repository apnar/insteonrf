"""Tests for the JSON-lines logger and the mesh-repeat deduper."""

import json
import time

import pytest

from insteonrf.monitor import Deduper, JsonlWriter, MqttPublisher, records
from insteonrf.packet import Packet


def on(hops_left=3, cmd2=0xFF):
    p = Packet.build("13.25.80", "16.3F.E5", cmd1=0x11, cmd2=cmd2, hops_left=hops_left)
    p.timestamp = time.time()
    return p


def test_dedupe_collapses_hop_repeats():
    """Repeats differ in hops-left and therefore in the CRC, but are one message."""
    recs = list(records([on(3), on(2), on(1), on(0)]))
    assert len(recs) == 1
    assert recs[0]["repeats"] == 4 and recs[0]["hops_seen"] == [3, 2, 1, 0]
    assert recs[0]["hops_left"] == 3  # the first sighting is what is reported


def test_dedupe_keeps_distinct_packets():
    recs = list(records([on(3), on(3, cmd2=0x00), on(2)]))
    assert [r["cmd2"] for r in recs] == [0xFF, 0x00]
    assert recs[0]["repeats"] == 2


def test_dedupe_window_expires():
    """A copy arriving after the window is a new message, not a repeat."""
    dd = Deduper(window_s=0.01)
    first = on()
    dd.add(first)
    assert dd.pop_ready(first.timestamp) == []          # still inside the window
    ready = dd.pop_ready(first.timestamp + 1.0)
    assert len(ready) == 1 and ready[0]["repeats"] == 1
    later = on()
    later.timestamp = first.timestamp + 1.0
    dd.add(later)
    assert len(dd) == 1 and dd.flush()[0]["repeats"] == 1


def test_dedupe_can_be_switched_off():
    recs = list(records([on(3), on(2)], dedupe=False))
    assert len(recs) == 2 and "repeats" not in recs[0]


def test_dedupe_flush_hands_over_pending_records():
    dd = Deduper()
    dd.add(on())
    dd.add(on())
    assert len(dd) == 1
    out = dd.flush()
    assert len(out) == 1 and out[0]["repeats"] == 2 and len(dd) == 0 and dd.flush() == []


def test_records_report_the_final_repeat_count():
    """Deferred emission is the point: the logged record counts every copy."""
    recs = list(records([on(3), on(2), on(1)]))
    assert len(recs) == 1 and recs[0]["repeats"] == 3


def test_jsonl_writer_appends_and_rotates(tmp_path):
    path = tmp_path / "rf.jsonl"
    w = JsonlWriter(path, max_bytes=400, backups=2)
    for _ in range(20):
        w.write(on().to_dict())
    w.close()
    assert path.exists() and (tmp_path / "rf.jsonl.1").exists()
    assert not (tmp_path / "rf.jsonl.3").exists()
    for line in path.read_text().splitlines():
        assert json.loads(line)["command"] == "On"


def test_jsonl_writer_creates_parent(tmp_path):
    w = JsonlWriter(tmp_path / "deep" / "dir" / "rf.jsonl")
    w.write({"a": 1})
    w.close()
    assert (tmp_path / "deep" / "dir" / "rf.jsonl").read_text().strip() == '{"a":1}'


def test_record_schema_is_json_serialisable():
    rec = list(records([on()]))[0]
    round_tripped = json.loads(json.dumps(rec))
    for key in ("time", "timestamp", "msg_type", "to", "from", "cmd1", "cmd2", "command",
                "crc_ok", "hops_left", "raw", "repeats", "hops_seen"):
        assert key in round_tripped
    assert Packet.from_dict(round_tripped).cmd1 == 0x11


def test_mqtt_publisher_needs_paho(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    monkeypatch.setitem(sys.modules, "paho", None)
    with pytest.raises(SystemExit):
        MqttPublisher("127.0.0.1")


def test_mqtt_publisher_publishes_per_device(monkeypatch):
    import sys
    import types

    published = []

    class FakeClient:
        def __init__(self, client_id=None):
            pass

        def username_pw_set(self, u, p):
            published.append(("auth", u))

        def connect_async(self, host, port):
            published.append(("connect", host, port))

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            published.append(("disconnect",))

        def publish(self, topic, payload, qos=0):
            published.append((topic, json.loads(payload)["cmd1"]))

    fake = types.ModuleType("paho.mqtt.client")
    fake.Client = FakeClient
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake)

    pub = MqttPublisher("broker", 1883, "insteon-rf/", username="u", password="p")
    pub.publish(list(records([on()]))[0])
    pub.close()
    topics = [x[0] for x in published]
    assert "insteon-rf" in topics and "insteon-rf/13.25.80" in topics
    assert ("connect", "broker", 1883) in published and ("disconnect",) in published


def test_dedupe_folds_the_real_capture_to_its_six_messages():
    """The rfcat capture is 3 queries + 3 ACKs, each repeated by the mesh.

    Repeats arrive with different amounts of trailing pad (whatever ended the
    receive block), so the key must ignore everything from the CRC onwards.
    """
    import pathlib

    from insteonrf.packet import parse_bits

    data = pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt"
    pkts = [p for line in data.read_text().splitlines() if line[:1] in "01"
            for p in parse_bits(line, 1000.0) if p.calc_crc is not None]
    recs = list(records(pkts))
    assert len(pkts) == 16 and len(recs) == 6
    assert sum(r["repeats"] for r in recs) == 16
    pairs = [(r["from"], r["to"]) for r in recs]
    assert pairs == [
        ("2B.93.07", "29.4E.52"), ("29.4E.52", "2B.93.07"),
        ("2B.93.07", "25.09.42"), ("25.09.42", "2B.93.07"),
        ("2B.93.07", "2B.A0.AB"), ("2B.A0.AB", "2B.93.07"),
    ]


def test_dedupe_tolerates_truncated_pads():
    """Two copies of one message with different pad lengths are one record."""
    full = on()
    short = Packet(full.data[:11], timestamp=full.timestamp, complete=False)
    recs = list(records([full, short]))
    assert len(recs) == 1 and recs[0]["repeats"] == 2
