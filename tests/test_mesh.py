"""Capture transport and the mesh service (``insteonrf.radio.mqtt``, ``insteonrf.mesh``).

Boards are the least trusted part of the system — a wedged one can publish
anything to a topic we subscribe to — so most of the transport tests feed
malformed payloads and check they are dropped rather than crashing the
service or reaching the decoder.
"""

from __future__ import annotations

import base64
import json
import threading
import time

import pytest

from insteonrf.inject import Injector, Tier
from insteonrf.mesh import MeshService, MissTable
from insteonrf.packet import START_HEADER, START_HEADER_INV, Packet
from insteonrf.plm import to_plm_bytes
from insteonrf.radio.mqtt import (
    MAX_CAPTURE_BYTES,
    Capture,
    bits_from_bytes,
    bytes_from_bits,
    with_sync_header,
)

PLM = "2B.93.07"
DEV = "29.4E.52"
LEAK = "44.12.AB"


def fifo_bits(p: Packet) -> str:
    """What a radio's FIFO holds: the on-air stream *after* the sync word.

    A radio consumes the pattern it synchronised on, so the start header is
    never in the buffer. Fixtures that include it are not what hardware
    delivers, and hid the bug this models.
    """
    b = p.to_bits()
    i = b.find(START_HEADER_INV)
    assert i >= 0
    return b[i + len(START_HEADER_INV):]


def capture_bytes(src=DEV, group=1, **kw) -> bytes:
    kw.setdefault("cmd1", 0x11)
    kw.setdefault("cmd2", 0xFF)
    p = Packet.build(src, group=group, bcast=True, **kw)
    return bytes_from_bits(fifo_bits(p))


def payload(raw: bytes, **over) -> bytes:
    rec = {
        "n": "insteon-rf-up",
        "seq": 1,
        "t": int(time.time() * 1000),
        "us": 12345,
        "rssi": -87.5,
        "len": len(raw),
        "b": base64.b64encode(raw).decode(),
    }
    rec.update(over)
    return json.dumps(rec).encode()


# --------------------------------------------------------------------------- bit expansion


def test_bits_from_bytes_is_msb_first():
    assert bits_from_bytes(b"\x80") == "10000000"
    assert bits_from_bytes(b"\x01") == "00000001"
    assert bits_from_bytes(b"\xcb\x54") == "1100101101010100"


def test_bit_byte_round_trip():
    data = bytes(range(256))
    assert bytes_from_bits(bits_from_bytes(data)) == data


def test_bytes_from_bits_pads_the_last_byte():
    assert bytes_from_bits("1") == b"\x80"
    assert bytes_from_bits("") == b""


def test_a_capture_survives_the_whole_transport():
    """Bytes on the board -> base64 -> bits -> a decoded packet."""
    original = Packet.build(DEV, group=4, bcast=True, cmd1=0x11, cmd2=0xFF)
    cap = Capture.from_payload("insteon-rf/rx/up", payload(bytes_from_bits(fifo_bits(original))))
    assert cap is not None
    from insteonrf.fusion import sightings_from_capture

    got = sightings_from_capture(cap.bits, cap.receiver)
    assert got and got[0].packet.crc_ok is True
    assert got[0].packet.group == 4


# --------------------------------------------------------------------------- the consumed header


def test_first_packet_in_a_capture_is_decoded():
    """Measured on the first Heltec: 3 of 4 captures decoded truncated or not
    at all, because the radio had consumed the start header and only a *later*
    packet in the buffer still had one. The first packet -- the one the radio
    actually synchronised on -- was being thrown away."""
    from insteonrf.fusion import sightings_from_capture

    # Models the real first capture: the PLM's Get Engine Version to a KPL.
    original = Packet.build(PLM, DEV, cmd1=0x0D, cmd2=0x00)
    raw = bytes_from_bits(fifo_bits(original))          # exactly the FIFO
    assert START_HEADER_INV not in bits_from_bytes(raw)  # header really is gone
    cap = Capture.from_payload("insteon-rf/rx/up", payload(raw))
    got = sightings_from_capture(cap.bits, cap.receiver)
    assert len(got) >= 1
    assert got[0].packet.crc_ok is True
    assert str(got[0].packet.from_addr) == PLM
    assert str(got[0].packet.to_addr) == DEV
    assert got[0].packet.cmd1 == 0x0D


def test_default_header_is_the_on_air_start_header():
    assert with_sync_header("0101") == START_HEADER_INV + "0101"


def test_header_follows_the_boards_sync_word_polarity():
    """A board flipped to the inverted sync word delivers inverted bits; the
    header put back must match, and then the parser normalises the lot."""
    from insteonrf.fusion import sightings_from_capture

    original = Packet.build(DEV, PLM, cmd1=0x0D, cmd2=0x00)
    inverted = "".join("1" if c == "0" else "0" for c in fifo_bits(original))
    cap = Capture.from_payload("insteon-rf/rx/up",
                               payload(bytes_from_bits(inverted), sw="CCCCCEAA"))
    assert cap is not None and cap.sync_word == 0xCCCCCEAA
    assert cap.bits.startswith(START_HEADER)
    got = sightings_from_capture(cap.bits, cap.receiver)
    assert got and got[0].packet.crc_ok is True and got[0].packet.cmd1 == 0x0D


def test_sw_accepts_int_or_hex_string_and_ignores_junk():
    raw = capture_bytes()
    assert Capture.from_payload("t/rx/a", payload(raw, sw=0x33333155)).sync_word == 0x33333155
    assert Capture.from_payload("t/rx/a", payload(raw, sw="33333155")).sync_word == 0x33333155
    cap = Capture.from_payload("t/rx/a", payload(raw, sw="not hex"))
    assert cap is not None and cap.sync_word is None
    assert cap.bits.startswith(START_HEADER_INV), "falls back to the default header"


def test_a_capture_that_already_has_a_header_is_not_doubled():
    """An older publisher that shipped the header itself must not end up with
    a junk packet in front of the real one."""
    from insteonrf.fusion import sightings_from_capture

    original = Packet.build(DEV, PLM, cmd1=0x0D, cmd2=0x00)
    with_hdr = START_HEADER_INV + fifo_bits(original)
    cap = Capture.from_payload("insteon-rf/rx/old", payload(bytes_from_bits(with_hdr)))
    assert cap.bits.count(START_HEADER_INV) == 1
    got = sightings_from_capture(cap.bits, cap.receiver)
    assert got[0].packet.crc_ok is True and got[0].packet.cmd1 == 0x0D


# --------------------------------------------------------------------------- payload parsing


def test_parses_a_good_payload():
    cap = Capture.from_payload("insteon-rf/rx/up", payload(capture_bytes()))
    assert cap is not None
    assert cap.receiver == "insteon-rf-up"
    assert cap.rssi_dbm == -87.5
    assert cap.seq == 1 and cap.micros == 12345


def test_receiver_name_falls_back_to_the_topic():
    """The topic is where the node name really lives."""
    cap = Capture.from_payload("insteon-rf/rx/basement", payload(capture_bytes(), n=None))
    assert cap is not None and cap.receiver == "basement"


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"not json",
        b"[1,2,3]",                       # not an object
        b'{"no_b_field": 1}',
        b'{"b": 12345}',                  # not a string
        b'{"b": "!!!not base64!!!"}',
        b'{"b": ""}',                     # empty capture
    ],
)
def test_malformed_payloads_are_dropped(bad):
    assert Capture.from_payload("insteon-rf/rx/up", bad) is None


def test_an_oversized_capture_is_dropped():
    """A board sending megabytes is broken, not informative."""
    big = base64.b64encode(b"\x00" * (MAX_CAPTURE_BYTES + 1)).decode()
    assert Capture.from_payload("insteon-rf/rx/up", json.dumps({"b": big}).encode()) is None


def test_a_board_with_an_unsynced_clock_does_not_file_captures_in_1970():
    cap = Capture.from_payload("insteon-rf/rx/up", payload(capture_bytes(), t=0))
    assert cap is not None
    assert abs(cap.timestamp - time.time()) < 5


def test_an_implausible_future_timestamp_is_ignored():
    future = int((time.time() + 86400) * 1000)
    cap = Capture.from_payload("insteon-rf/rx/up", payload(capture_bytes(), t=future))
    assert cap is not None
    assert abs(cap.timestamp - time.time()) < 5


def test_a_plausible_timestamp_is_kept():
    when = time.time() - 0.4
    cap = Capture.from_payload("insteon-rf/rx/up", payload(capture_bytes(), t=int(when * 1000)))
    assert cap is not None
    assert cap.timestamp == pytest.approx(when, abs=0.01)


# --------------------------------------------------------------------------- miss table


def fused(src=LEAK, group=1, *, plm_saw=None, receiver="a", rssi=-70, t=100.0):
    from insteonrf.fusion import Fusion, sightings_from_capture

    p = Packet.build(src, group=group, bcast=True, cmd1=0x11, cmd2=0xFF)
    f = Fusion()
    for s in sightings_from_capture(p.to_bits(), receiver, rssi_dbm=rssi, timestamp=t):
        f.add(s)
    (e,) = f.flush(t + 10)
    e.plm_saw_it = plm_saw
    return e


def test_miss_table_counts_both_paths():
    t = MissTable()
    t.note(fused(plm_saw=True))
    t.note(fused(plm_saw=False))
    t.note(fused(plm_saw=False), injected=True)
    d = t.devices[LEAK]
    assert d.heard_by_rf == 3
    assert d.plm_also_heard == 1
    assert d.plm_missed == 2
    assert d.injected == 1
    assert d.miss_rate == pytest.approx(2 / 3)


def test_miss_table_records_receivers_and_best_rssi():
    t = MissTable()
    t.note(fused(receiver="up", rssi=-95, plm_saw=False))
    t.note(fused(receiver="basement", rssi=-71, plm_saw=False))
    d = t.devices[LEAK]
    assert d.receivers == {"up", "basement"}
    assert d.best_rssi_dbm == -71


def test_miss_table_report_sorts_worst_first():
    t = MissTable()
    for _ in range(4):
        t.note(fused(src="11.11.11", plm_saw=True))
    t.note(fused(src="22.22.22", plm_saw=False))
    report = t.report()
    lines = [ln for ln in report.splitlines() if ln.startswith(("11.", "22."))]
    assert lines[0].startswith("22.22.22"), "the device the PLM misses comes first"
    assert "100.0%" in lines[0]


def test_miss_table_min_seen_filters_noise():
    t = MissTable()
    t.note(fused(src="11.11.11", plm_saw=False))
    assert "11.11.11" in t.report(min_seen=1)
    assert "11.11.11" not in t.report(min_seen=2)


def test_unknown_plm_status_counts_as_neither():
    t = MissTable()
    t.note(fused(plm_saw=None))
    d = t.devices[LEAK]
    assert d.heard_by_rf == 1 and d.plm_also_heard == 0 and d.plm_missed == 0


# --------------------------------------------------------------------------- service


class FakeReceiver:
    """Stands in for MqttReceiver without a broker."""

    name = "fake"
    exhausted = False

    def __init__(self, captures=()):
        self.queue = list(captures)
        self.last_seq: dict[str, int] = {}
        self.gaps = 0
        self.dropped = 0

    def next_capture(self, timeout_ms=0):
        return self.queue.pop(0) if self.queue else None

    def close(self):
        pass


class FakePlm:
    def __init__(self, healthy=True):
        self._healthy = healthy
        self.frames = 0
        self.published = []
        self.on_frame = None

    def healthy(self, **_):
        return self._healthy

    def publish_inject(self, frame, event):
        self.published.append(frame)

    def close(self):
        pass


def cap_for(src=LEAK, group=1, receiver="up", t=100.0) -> Capture:
    p = Packet.build(src, group=group, bcast=True, cmd1=0x11, cmd2=0xFF)
    return Capture(receiver=receiver, bits=p.to_bits(), timestamp=t, rssi_dbm=-70, seq=1)


def test_service_decodes_and_fuses():
    svc = MeshService(FakeReceiver())
    assert svc.handle_capture(cap_for()) == 1
    assert svc.packets == 1
    events = svc.drain(now=200.0)
    assert len(events) == 1
    assert svc.events == 1


def test_service_writes_and_reports():
    svc = MeshService(FakeReceiver())
    svc.handle_capture(cap_for())
    svc.drain(now=200.0)
    assert LEAK in svc.misses.devices


def test_service_injects_when_the_plm_link_is_healthy():
    plm = FakePlm(healthy=True)
    inj = Injector(publish=plm.publish_inject, plm_addr=PLM, battery_addrs=[LEAK],
                   allow=[Tier.BATTERY], shadow=False)
    svc = MeshService(FakeReceiver(), injector=inj, plm=plm)
    svc.handle_capture(cap_for())
    svc.drain(now=200.0)
    assert len(plm.published) == 1


def test_service_refuses_to_inject_without_the_plm_mirror():
    """Blind injection would double-process: we could not tell what it heard."""
    plm = FakePlm(healthy=False)
    inj = Injector(publish=plm.publish_inject, plm_addr=PLM, battery_addrs=[LEAK],
                   allow=[Tier.BATTERY], shadow=False)
    svc = MeshService(FakeReceiver(), injector=inj, plm=plm)
    svc.handle_capture(cap_for())
    svc.drain(now=200.0)
    assert plm.published == []
    assert inj.counters.injected == 0


def test_service_suppresses_what_the_plm_reported():
    plm = FakePlm(healthy=True)
    inj = Injector(publish=plm.publish_inject, plm_addr=PLM, battery_addrs=[LEAK],
                   allow=[Tier.BATTERY], shadow=False)
    svc = MeshService(FakeReceiver(), injector=inj, plm=plm)
    # The patched insteon-mqtt reports the same message first.
    p = Packet.build(LEAK, group=1, bcast=True, cmd1=0x11, cmd2=0xFF)
    inj.note_plm(to_plm_bytes(p), now=199.9)
    svc.handle_capture(cap_for())
    svc.drain(now=200.0)
    assert plm.published == []
    assert inj.counters.refused["the PLM already heard it"] == 1


def test_service_and_injector_share_one_record_of_what_the_plm_heard():
    plm = FakePlm()
    inj = Injector(plm_addr=PLM, allow=[Tier.BATTERY])
    svc = MeshService(FakeReceiver(), injector=inj, plm=plm)
    assert plm.on_frame == svc.memory.note
    assert inj.memory is svc.memory, "suppression and the miss table must agree"


def test_the_plm_comparison_works_without_an_injector():
    """Phase 3 measures the miss rate *before* injection is enabled.

    If plm_saw_it were only set inside the injector, measuring would require
    turning on the very thing the measurement is supposed to justify.
    """
    plm = FakePlm()
    svc = MeshService(FakeReceiver(), plm=plm)
    assert plm.on_frame == svc.memory.note

    p = Packet.build(LEAK, group=1, bcast=True, cmd1=0x11, cmd2=0xFF)
    svc.memory.note(to_plm_bytes(p), now=199.9)
    svc.handle_capture(cap_for())
    (event,) = svc.drain(now=200.0)
    assert event.plm_saw_it is True
    assert svc.misses.devices[LEAK].plm_also_heard == 1


def test_a_message_the_plm_missed_is_recorded_as_missed():
    svc = MeshService(FakeReceiver(), plm=FakePlm())
    svc.handle_capture(cap_for())
    (event,) = svc.drain(now=200.0)
    assert event.plm_saw_it is False
    assert svc.misses.devices[LEAK].plm_missed == 1


def test_undecodable_fragments_do_not_inflate_the_miss_table():
    """A four-byte fragment looks like a packet start but names no sender."""
    from insteonrf.fusion import Fusion, Sighting
    from insteonrf.packet import parse_bits

    svc = MeshService(FakeReceiver())
    p = Packet.build(LEAK, group=1, bcast=True, cmd1=0x11, cmd2=0xFF)
    truncated = parse_bits(p.to_bits())[0]
    truncated.data = truncated.data[:5]          # below the CRC
    svc.fusion = Fusion()
    svc.fusion.add(Sighting(truncated, "up", -70, 100.0))
    svc.drain(now=200.0)
    assert svc.misses.devices == {}
    assert svc.misses.undecodable == 1
    assert "undecodable" in svc.misses.report()


def test_run_stops_on_the_event():
    stop = threading.Event()
    stop.set()
    svc = MeshService(FakeReceiver([cap_for()]))
    svc.run(stop, poll_ms=1)  # returns immediately
    assert svc.captures == 0


def test_stats_are_reportable():
    plm = FakePlm()
    inj = Injector(plm_addr=PLM, allow=[Tier.BATTERY])
    svc = MeshService(FakeReceiver(), injector=inj, plm=plm)
    svc.handle_capture(cap_for())
    svc.drain(now=200.0)
    st = svc.stats()
    assert st["captures"] == 1 and st["packets"] == 1 and st["events"] == 1
    assert "injector" in st and "plm_link_healthy" in st
    json.dumps(st)  # must be serialisable for the log


# --------------------------------------------------------------------------- dongle as a member


def test_dongle_capture_payload_is_what_the_mesh_consumes():
    """Closes the loop between monitor --mesh-capture and insteon-rf mesh.

    The rfcat dongle is a valid mesh receiver, and the only one that can
    produce soft decisions, so it stays useful after the boards arrive. If
    these two formats drift the dongle silently stops contributing.
    """
    from insteonrf.monitor import MqttPublisher

    original = Packet.build(DEV, group=4, bcast=True, cmd1=0x11, cmd2=0xFF)
    # receive_bits() hands monitor the consumed header put back on the front.
    bits = START_HEADER_INV + fifo_bits(original)

    sent = {}

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            sent["topic"] = topic
            sent["payload"] = payload.encode() if isinstance(payload, str) else payload

    pub = MqttPublisher.__new__(MqttPublisher)   # no broker needed
    pub.topic = "insteon-rf"
    pub.client = FakeClient()
    pub.captures = 0

    pub.publish_capture(bits, receiver="dongle", timestamp=time.time(), rssi_dbm=-93.0, seq=7)
    assert sent["topic"] == "insteon-rf/rx/dongle"

    rec = json.loads(sent["payload"])
    assert rec["sw"] == "3155", "the dongle names the 16-bit header it synced on"
    assert not bits_from_bytes(base64.b64decode(rec["b"])).startswith(START_HEADER_INV), \
        "published bytes are FIFO content: the header is the consumer's job"

    cap = Capture.from_payload(sent["topic"], sent["payload"])
    assert cap is not None
    assert cap.receiver == "dongle" and cap.seq == 7 and cap.rssi_dbm == -93.0

    from insteonrf.fusion import sightings_from_capture

    got = sightings_from_capture(cap.bits, cap.receiver)
    assert got, "the mesh must be able to decode what the dongle published"
    assert got[0].packet.crc_ok is True
    assert got[0].packet.group == 4


def test_the_modem_is_kept_out_of_its_own_miss_table():
    """Measured live: without this the PLM leads every report at 100% missed.

    The modem's own transmissions are heard on RF but never come back as
    inbound messages -- it reports them as 0x62 echoes -- so they look like
    the misses of a device that misses everything. They are also the one
    thing that must never be injected.
    """
    t = MissTable(plm_addr=PLM)
    t.note(fused(src=PLM, plm_saw=False))
    t.note(fused(src=LEAK, plm_saw=False))
    assert PLM not in t.devices
    assert t.plm_own_transmissions == 1
    assert t.devices[LEAK].plm_missed == 1
    assert "from the modem itself" in t.report()


def test_the_service_takes_the_plm_address_from_the_injector():
    inj = Injector(plm_addr=PLM, allow=[Tier.BATTERY])
    svc = MeshService(FakeReceiver(), injector=inj, plm=FakePlm())
    assert svc.misses.plm_addr is not None
    assert str(svc.misses.plm_addr) == PLM
