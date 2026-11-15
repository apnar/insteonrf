"""Contract v1 (insteonrf/listener.py): decode, fold, serialise."""

from __future__ import annotations

import json

import pytest

from insteonrf.listener import CONTRACT_VERSION, Folder, ListenerMessage, capture_messages
from insteonrf.packet import Packet
from insteonrf.plm import to_plm_bytes

PLM = "2B.93.07"


def _hop(hops_left: int) -> Packet:
    """One ACK as the sender and its repeaters transmit it."""
    return Packet.build("29.4E.52", PLM, cmd1=0x0D, cmd2=0x02, ack=True,
                        max_hops=3, hops_left=hops_left)


def _air(*pkts: Packet) -> str:
    """Several packets back to back as one capture would hold them."""
    return "".join(p.to_bits() for p in pkts)


def test_capture_folds_hop_copies() -> None:
    copies = [_hop(h) for h in (3, 2, 1)]
    msgs = capture_messages(_air(*copies), "embed", 100.0, rssi=-80.0)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.copies == 3
    assert m.hops_left == 3
    assert m.frame == to_plm_bytes(copies[0])
    assert m.frame[:2] == b"\x02\x50"
    assert m.rssi == -80.0
    assert m.repaired == 0


def test_group_broadcast_uses_plm_layout() -> None:
    pkt = Packet.build("3F.4E.D6", group=4, cmd1=0x11, cmd2=0x04, bcast=True)
    (m,) = capture_messages(_air(pkt), "embed", 1.0)
    # PLM frame: from (the sender) first, then the "to" field (group in its low byte).
    assert m.frame[2:5] == bytes.fromhex("3F4ED6")
    assert m.frame[7] == 4


def test_extended_message() -> None:
    pkt = Packet.build("29.4E.52", PLM, cmd1=0x2F, cmd2=0x00, ext_data=[1, 2, 3])
    (m,) = capture_messages(_air(pkt), "embed", 1.0)
    assert m.frame[1] == 0x51
    assert len(m.frame) == 25


def test_bad_crc_is_not_published() -> None:
    pkt = Packet.build("29.4E.52", PLM, cmd1=0x11, crc=0x00)
    assert capture_messages(_air(pkt), "embed", 1.0) == []


def test_distinct_messages_stay_distinct() -> None:
    a = Packet.build("29.4E.52", PLM, cmd1=0x11)
    b = Packet.build("29.4E.52", PLM, cmd1=0x13)
    assert len(capture_messages(_air(a, b), "embed", 1.0)) == 2


def test_folder_window_and_retry() -> None:
    f = Folder("v3", window_s=0.5)
    pkt = Packet.build("29.4E.52", PLM, cmd1=0x11)
    f.add(pkt, t=10.0)
    f.add(pkt, t=10.1)
    assert f.pop_ready(now=10.3) == []
    # A retry 0.7 s later is a new transmission; the first one is released.
    f.add(pkt, t=10.7)
    first = f.pop_ready(now=10.8)
    assert [m.copies for m in first] == [2]
    assert [m.t for m in f.pop_ready(now=11.3)] == [10.7]


def test_folder_prefers_clean_copy() -> None:
    f = Folder("v3")
    rep = Packet.build("29.4E.52", PLM, cmd1=0x11, hops_left=3)
    rep.corrected = 1
    clean = Packet.build("29.4E.52", PLM, cmd1=0x11, hops_left=2)
    f.add(rep, t=1.0)
    f.add(clean, t=1.05)
    (m,) = f.flush()
    assert m.repaired == 0 and m.hops_left == 2 and m.t == 1.0


def test_json_round_trip() -> None:
    (m,) = capture_messages(_air(Packet.build("29.4E.52", PLM)), "embed", 1794759872.4291)
    d = json.loads(m.to_json())
    assert d["v"] == CONTRACT_VERSION
    assert d["frame"] == m.frame.hex().upper()
    assert d["t"] == 1794759872.429
    assert ListenerMessage.from_dict(d).frame == m.frame
    with pytest.raises(ValueError):
        ListenerMessage.from_dict(dict(d, v=99))


@pytest.mark.parametrize("name", ["embed-vectors.jsonl", "synthetic-vectors.jsonl"])
def test_contract_vectors_match_reference(name: str) -> None:
    """The vector files the ESPHome decoder is tested against still agree
    with this reference (regenerate them if the contract changes)."""
    import pathlib

    from insteonrf.radio.mqtt import Capture

    path = pathlib.Path(__file__).parent / "data" / "contract-v1" / name
    lines = path.read_text().splitlines()
    assert lines
    for line in lines:
        case = json.loads(line)
        cap = Capture.from_payload("insteon-rf/rx/x", json.dumps(case["capture"]).encode())
        assert cap is not None
        got = capture_messages(cap.bits, "x", cap.timestamp, sync_word=cap.sync_word)
        assert [{"frame": m.frame.hex().upper(), "copies": m.copies, "hops_left": m.hops_left}
                for m in got] == case["expected"], case.get("name", case["capture"].get("seq"))
