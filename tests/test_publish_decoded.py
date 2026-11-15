"""monitor --publish-decoded: listener contract v1 from the host radios.

The dongle and the RTL-SDR pods decode on their own and publish one message
per transmission to ``<topic>/msg/<rx>`` (insteonrf/listener.py,
Doc/REARCHITECTURE.md). No hardware here: the radios are FileRadio
subclasses handed to ``monitor`` through ``cli._open_radio``, so nothing can
open, reset or transmit on the real dongle.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import sys
import time
from typing import Any

import numpy as np
import pytest

import insteonrf.monitor as mon
from insteonrf import cli
from insteonrf.dsp import Burst
from insteonrf.monitor import RELEASE_LATENESS_S, DecodedPublisher, packet_air_times
from insteonrf.packet import Packet, find_headers, parse_bits
from insteonrf.plm import to_plm_bytes
from insteonrf.radio import FileRadio
from insteonrf.radio.file import meta_time
from insteonrf.recover import recover_packets

PLM = "2B.93.07"
BAUD = 9124
#: A symbol of a standard packet's data whose flip hard-bit repair undoes
#: (an illegal Manchester pair inside the cmd2 frame).
REPAIRABLE = 179


def ack(hops_left: int) -> Packet:
    """An ACK from 29.4E.52 to the PLM, as the sender and its repeaters send it."""
    return Packet.build("29.4E.52", PLM, cmd1=0x0D, cmd2=0x02, ack=True,
                        max_hops=3, hops_left=hops_left)


def on_cmd() -> Packet:
    return Packet.build("29.4E.52", PLM, cmd1=0x11, cmd2=0xFF, max_hops=3, hops_left=3)


def flip(bits: str, i: int) -> str:
    return bits[:i] + ("1" if bits[i] == "0" else "0") + bits[i + 1:]


def run(argv: list[str]) -> int:
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return cli.monitor_main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err


class FakePublisher:
    """Stands in for MqttPublisher; records what monitor publishes."""

    instances: list[FakePublisher] = []

    def __init__(self, host: str, port: int = 1883, topic: str = "insteon-rf", **kw: Any):
        self.topic = topic
        self.published = self.alerts = self.captures = 0
        self.messages: list[tuple[float, str, dict[str, Any]]] = []
        self.capture_receivers: list[str] = []
        self.alert_kinds: list[str] = []
        self.closed = False
        FakePublisher.instances.append(self)

    def publish(self, rec: dict[str, Any]) -> None:
        self.published += 1

    def publish_message(self, topic: str, payload: str) -> None:
        self.messages.append((time.time(), topic, json.loads(payload)))

    def publish_capture(self, bits: str, *, receiver: str, **kw: Any) -> None:
        self.captures += 1
        self.capture_receivers.append(receiver)

    def publish_alert(self, alert: dict[str, Any]) -> None:
        self.alerts += 1
        self.alert_kinds.append(alert["alert"])

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_pub(monkeypatch: pytest.MonkeyPatch) -> type[FakePublisher]:
    FakePublisher.instances = []
    monkeypatch.setattr(mon, "MqttPublisher", FakePublisher)
    return FakePublisher


@pytest.fixture(autouse=True)
def _clear_stop() -> Any:
    yield
    cli.STOP.clear()


# --------------------------------------------------------------------------- DecodedPublisher


def test_hop_copies_fold_into_one_message_on_the_contract_topic() -> None:
    sent: list[tuple[str, str]] = []
    now = [100.0]
    pub = DecodedPublisher("dongle", lambda t, p: sent.append((t, p)), clock=lambda: now[0])
    for i, h in enumerate((3, 2, 1)):
        pub.add(ack(h), t=100.0 + 0.05 * i)
    now[0] = 100.6
    assert pub.tick() == [] and sent == []
    now[0] = 100.0 + 0.5 + RELEASE_LATENESS_S + 0.01
    (msg,) = pub.tick()
    (topic, payload), = sent
    assert topic == "insteon-rf/msg/dongle"
    d = json.loads(payload)
    assert d == {"v": 1, "rx": "dongle", "t": 100.0,
                 "frame": to_plm_bytes(ack(3)).hex().upper(), "copies": 3,
                 "hops_left": 3, "rssi": None, "snr": None, "repaired": 0}
    assert pub.published == 1 and len(pub) == 0
    assert pub.tick() == []


def test_topic_follows_the_prefix() -> None:
    sent: list[tuple[str, str]] = []
    pub = DecodedPublisher("v3", lambda t, p: sent.append((t, p)), prefix="lab/")
    pub.add(ack(3), t=1.0)
    pub.flush()
    assert pub.topic == "lab/msg/v3" and sent[0][0] == "lab/msg/v3"


def test_repaired_snr_and_rssi_reach_the_payload() -> None:
    sent: list[str] = []
    pub = DecodedPublisher("v3", lambda t, p: sent.append(p))
    pkt = on_cmd()
    pkt.corrected, pkt.snr_db, pkt.rssi_dbm = 1, 17.26, -71.04
    pub.add(pkt, t=5.0)
    pub.flush()
    d = json.loads(sent[0])
    assert (d["repaired"], d["snr"], d["rssi"]) == (1, 17.3, -71.0)


def test_rssi_can_be_withheld_without_touching_the_packet() -> None:
    """The dongle's RSSI is a register snapshot after the block, not the packet's."""
    sent: list[str] = []
    pub = DecodedPublisher("dongle", lambda t, p: sent.append(p), rssi=False)
    pkt = on_cmd()
    pkt.rssi_dbm = -104.0
    pub.add(pkt, t=5.0)
    pub.flush()
    assert json.loads(sent[0])["rssi"] is None
    assert pkt.rssi_dbm == -104.0  # the pod's own log keeps it


def test_the_gate_refuses_bad_packets() -> None:
    sent: list[str] = []
    pub = DecodedPublisher("dongle", lambda t, p: sent.append(p))
    pub.add(Packet.build("29.4E.52", PLM, cmd1=0x11, crc=0x00), t=1.0)
    assert pub.flush() == [] and pub.rejected == 1 and sent == []


def test_data_clock_ignores_the_wall_clock() -> None:
    """Replay: windows close on the recording's own time, not on how fast it is read."""
    pub = DecodedPublisher("dongle", lambda t, p: None, data_clock=True,
                           clock=lambda: 1e12)
    pub.add(ack(3), t=10.0)
    assert pub.tick() == []
    pub.add(on_cmd(), t=10.6)  # the next transmission moves the clock on
    assert [m.t for m in pub.tick()] == [10.0]
    assert [m.t for m in pub.flush()] == [10.6]


def test_air_times_place_each_packet_in_its_block() -> None:
    """One dongle block holds several hop slots; each copy gets its own time."""
    a, b = on_cmd(), ack(3)
    bits = a.to_bits() + b.to_bits()
    _, offsets = find_headers(bits)
    pkts = parse_bits(bits, 50.0)
    assert len(pkts) == 2 and offsets[1] > offsets[0]
    assert packet_air_times(bits, pkts, 50.0, BAUD) == [50.0 + o / BAUD for o in offsets]
    # A repaired copy is located the same way.
    bad = flip(a.to_bits(), REPAIRABLE) + b.to_bits()
    (rec,) = [r.packet for r in recover_packets(bad, None, 50.0) if r.corrected]
    assert packet_air_times(bad, [rec], 50.0, BAUD) == [50.0 + offsets[0] / BAUD]


# --------------------------------------------------------------------------- MqttPublisher


def test_mqtt_publish_message_is_qos0_unretained_and_ignores_alerts_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    sent: list[tuple[str, str, int, bool]] = []

    class FakeClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def connect_async(self, host: str, port: int) -> None:
            pass

        def loop_start(self) -> None:
            pass

        def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
            sent.append((topic, payload, qos, retain))

    fake = types.ModuleType("paho.mqtt.client")
    fake.Client = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake)
    pub = mon.MqttPublisher("broker", alerts_only=True)
    pub.publish_message("insteon-rf/msg/dongle", '{"v":1}')
    assert sent == [("insteon-rf/msg/dongle", '{"v":1}', 0, False)]
    assert pub.messages == 1


# --------------------------------------------------------------------------- monitor


def _stamp(t: float) -> str:
    return "# " + _dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds") + " len=0"


T0 = _dt.datetime(2026, 11, 15, 10, 0, 0).timestamp()


def _recording(tmp_path: Any) -> Any:
    """Dongle-style blocks with recorded times: a message whose third copy is
    in the next block, a retry 2 s later, and a lone copy that needs repair."""
    path = tmp_path / "air.txt"
    path.write_text("\n".join([
        _stamp(T0), ack(3).to_bits() + ack(2).to_bits(),
        _stamp(T0 + 0.23), ack(1).to_bits(),
        _stamp(T0 + 2.0), ack(3).to_bits(),
        _stamp(T0 + 5.0), flip(on_cmd().to_bits(), REPAIRABLE),
    ]) + "\n")
    return path


def test_file_radio_replays_recorded_times(tmp_path: Any) -> None:
    path = _recording(tmp_path)
    got = list(FileRadio(path).iter_bits())
    assert [round(t - T0, 3) for t, _ in got] == [0.0, 0.23, 2.0, 5.0]
    assert meta_time("# 1794759872.429 len=3") == 1794759872.429
    assert meta_time("# start header at 15") is None
    assert meta_time("# 3 bursts") is None


def test_replay_publishes_folded_repaired_messages_deterministically(
        tmp_path: Any, fake_pub: type[FakePublisher]) -> None:
    path = str(_recording(tmp_path))
    base = ["--backend", "file", "--replay", path, "--mqtt", "broker", "--quiet",
            "--publish-decoded", "dongle"]
    assert run(base) == 0
    assert run([*base, "--queue", "1"]) == 0
    a, b = (p.messages for p in fake_pub.instances)
    assert [(t, d) for _, t, d in a] == [(t, d) for _, t, d in b]
    assert {t for _, t, _ in a} == {"insteon-rf/msg/dongle"}
    msgs = [d for _, _, d in a]
    head = find_headers(ack(3).to_bits())[1][0] / BAUD
    assert [(m["copies"], m["hops_left"], m["repaired"]) for m in msgs] == [
        (3, 3, 0), (1, 3, 0), (1, 3, 1)]
    assert [m["t"] for m in msgs] == [round(T0 + d + head, 3) for d in (0.0, 2.0, 5.0)]
    assert msgs[0]["frame"] == to_plm_bytes(ack(3)).hex().upper()
    assert msgs[2]["frame"] == to_plm_bytes(on_cmd()).hex().upper()
    assert all(m["rx"] == "dongle" and m["v"] == 1 for m in msgs)


def test_messages_go_out_on_quiet_air_without_another_packet(
        monkeypatch: pytest.MonkeyPatch, fake_pub: type[FakePublisher]) -> None:
    """The worker's short timer releases a message ~window+lateness after its
    first copy; it does not wait for the next block or for shutdown."""
    state: dict[str, float] = {}

    class QuietRadio(FileRadio):
        lossless = False
        reads = 0

        def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
            self.reads += 1
            if self.reads == 1:
                state["t0"] = time.time()
                return state["t0"], ack(3).to_bits() + ack(2).to_bits()
            time.sleep(timeout_ms / 1000)
            pub = fake_pub.instances[0]
            if pub.messages and "seen" not in state:
                state["seen"] = pub.messages[0][0]
            if "seen" in state or self.reads > 60:
                state["stop"] = time.time()
                cli.STOP.set()
            return None

    monkeypatch.setattr(cli, "_open_radio", lambda *a, **k: QuietRadio(lines=[]))
    assert run(["--mqtt", "broker", "--quiet", "--publish-decoded", "dongle",
                "--idle-rearm", "0", "--timeout", "50", "--max-silence", "0"]) == 0
    assert "seen" in state, "released only at shutdown"
    lag = state["seen"] - state["t0"]
    assert 0.5 + RELEASE_LATENESS_S <= lag < 1.5
    (msg,) = [d for _, _, d in fake_pub.instances[0].messages]
    assert msg["copies"] == 2


def test_soft_decisions_repair_and_carry_snr_and_rssi(
        monkeypatch: pytest.MonkeyPatch, fake_pub: type[FakePublisher]) -> None:
    """The SDR path: soft framing repairs the copy, snr and the radio's RSSI
    (for a backend whose RSSI is per packet) reach the message."""
    # A whole Manchester pair inverted is a legal pair with the wrong bit --
    # one flipped symbol alone, soft combining already gets right unrepaired.
    pair = REPAIRABLE - 1
    bits = flip(flip(on_cmd().to_bits(), pair), pair + 1)
    soft = np.array([1.0 if c == "1" else -1.0 for c in bits], dtype=np.float32)
    soft[pair:pair + 2] *= 0.05
    head = find_headers(bits)[1][0]
    bursts = [Burst(bits=bits, soft=soft, sps=4.0, cfo_hz=500.0, snr_db=12.34,
                    header_index=head, timestamp=T0)]

    class SdrLike(FileRadio):
        lossless = True

        def receive_burst(self, timeout_ms: int = 2000) -> Burst | None:
            if not bursts:
                self.exhausted = True
                return None
            return bursts.pop(0)

        def read_rssi(self) -> float:
            return -60.0

    monkeypatch.setattr(cli, "_open_radio", lambda *a, **k: SdrLike(lines=[]))
    assert run(["--backend", "rtlsdr", "--mqtt", "broker", "--quiet",
                "--publish-decoded", "v3", "--max-silence", "0"]) == 0
    ((_, topic, msg),) = fake_pub.instances[0].messages
    assert topic == "insteon-rf/msg/v3"
    assert (msg["repaired"], msg["snr"], msg["rssi"]) == (1, 12.3, -60.0)
    assert msg["t"] == round(T0 + head / BAUD, 3)
    assert msg["frame"] == to_plm_bytes(on_cmd()).hex().upper()


def test_dongle_rssi_is_not_published(monkeypatch: pytest.MonkeyPatch,
                                      fake_pub: type[FakePublisher]) -> None:
    class Dongle(FileRadio):
        def read_rssi(self) -> float:
            return -104.0

    radio = Dongle(lines=[ack(3).to_bits()])
    monkeypatch.setattr(cli, "_open_radio", lambda *a, **k: radio)
    assert run(["--mqtt", "broker", "--quiet", "--publish-decoded", "dongle",
                "--max-silence", "0", "--idle-rearm", "0"]) == 0
    ((_, _, msg),) = fake_pub.instances[0].messages
    assert msg["rssi"] is None


def test_works_beside_the_all_on_watch_and_mesh_captures(
        tmp_path: Any, fake_pub: type[FakePublisher]) -> None:
    group0 = Packet.build("3F.4E.D6", group=0, cmd1=0x11, cmd2=0x00, bcast=True)
    path = tmp_path / "air.txt"
    path.write_text(f"{_stamp(T0)}\n{group0.to_bits()}\n{_stamp(T0 + 1)}\n{ack(3).to_bits()}\n")
    assert run(["--backend", "file", "--replay", str(path), "--mqtt", "broker", "--quiet",
                "--watch-all-on", "--mesh-capture", "dongle",
                "--publish-decoded", "dongle"]) == 0
    pub = fake_pub.instances[0]
    assert pub.capture_receivers == ["dongle", "dongle"]
    assert pub.alerts >= 1
    assert len(pub.messages) == 2
    assert pub.closed


def test_publish_decoded_needs_a_broker(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.monitor_main(["--backend", "file", "--publish-decoded", "dongle"])
    assert exc.value.code == 2
    assert "--publish-decoded needs --mqtt" in capsys.readouterr().err
