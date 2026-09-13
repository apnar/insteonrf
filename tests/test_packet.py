import json
import pathlib

import pytest

from insteonrf.cmds import Command
from insteonrf.debug import dump_frames
from insteonrf.packet import (
    START_HEADER,
    START_HEADER_INV,
    Address,
    Flags,
    MsgType,
    Packet,
    decode_frames,
    ext_crc,
    parse_bits,
    pkt_crc,
)

DATA = pathlib.Path(__file__).parent / "data"

# (packet crc, wire bytes) — captured from real devices, from the original self-test
CRC_VECTORS = [
    (0x93, [0x07, 0x11, 0x78, 0x2B, 0x80, 0x25, 0x13, 0x11, 0x01, 0x93, 0x00, 0x00]),
    (0x58, [0x2B, 0x80, 0x25, 0x13, 0x11, 0x78, 0x2B, 0x11, 0xF6, 0x58]),
    (0xAF, [0x1B, 0x35, 0x02, 0x2B, 0x80, 0x25, 0x13, 0x2F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xD0, 0xAF]),
    (0xD3, [0x15, 0x80, 0x25, 0x13, 0x11, 0x78, 0x2B, 0x2F, 0x00, 0x00, 0x01, 0x0F, 0xFF, 0x00,
            0xA2, 0x00, 0x13, 0x25, 0x80, 0xFF, 0x1F, 0x01, 0x49, 0xD3, 0xAA, 0xDD, 0xAA, 0xAA,
            0xAA, 0xAA, 0xAA, 0xAA]),
]


@pytest.mark.parametrize("crc,data", CRC_VECTORS)
def test_pkt_crc(crc, data):
    assert pkt_crc(data) == crc


@pytest.mark.parametrize("crc,data", [v for v in CRC_VECTORS if len(v[1]) > 20])
def test_ext_crc(crc, data):
    assert ext_crc(data) == data[22]


def test_doc_example_crc():
    # Doc/pkt_format.md: 0B E5 3F 16 80 25 13 11 BF -> 5F
    assert pkt_crc([0x0B, 0xE5, 0x3F, 0x16, 0x80, 0x25, 0x13, 0x11, 0xBF]) == 0x5F


def test_addresses():
    a = Address("16.3F.E5")
    assert a.bytes == (0x16, 0x3F, 0xE5) and a.wire == (0xE5, 0x3F, 0x16)
    assert a.value == 0x163FE5 and str(a) == "16.3F.E5" and repr(a) == "Address('16.3F.E5')"
    assert a == Address("163fe5") == Address("16:3F:E5") == Address(0x163FE5) == Address(a)
    assert Address.from_wire([0xE5, 0x3F, 0x16]) == a
    assert Address([0x16, 0x3F, 0xE5]) == a
    for bad in ("1234", "16.3F.EZ", -1, 1 << 24):
        with pytest.raises(ValueError):
            Address(bad)
    with pytest.raises(TypeError):
        Address(True)


def test_address_interop_with_strings():
    """Address compares and hashes like its string form, so old code keeps working."""
    a = Address("16.3F.E5")
    assert a == "16.3F.E5" and "16.3f.e5" == a and a != "13.25.80"
    assert {a: 1}["16.3F.E5"] == 1 and a in {"16.3F.E5"}
    assert f"{a}" == "16.3F.E5" and f"{a:>12}" == "    16.3F.E5"
    assert (a == 0x163FE5) is False  # ints are not addresses for equality


def test_flags_roundtrip():
    for byte in range(256):
        f = Flags.from_byte(byte)
        assert f.to_byte() == byte
    f = Flags.from_byte(0x9F)
    assert f.msg_type is MsgType.BROADCAST and f.extended and f.group is False
    assert f.hops_left == 3 and f.max_hops == 3 and "Broadcast" in str(f)


def test_msg_type_and_command_enums():
    p = Packet.build("13.25.80", "16.3F.E5", cmd1=0x11, cmd2=0xFF)
    assert p.msg_type is MsgType.DIRECT and p.msg_type.label == "Direct"
    assert p.command is Command.ON and p.command.label == "On"
    assert p.flags.to_byte() == p.flags_byte
    assert Packet.build("13.25.80", "16.3F.E5", cmd1=0xC7).command is None


def test_build_direct_matches_original():
    p = Packet.build("13.25.80", "2B.78.11", cmd1=0x11, cmd2=0x01, max_hops=3, hops_left=1, pad=False)
    assert p.data == [0x07, 0x11, 0x78, 0x2B, 0x80, 0x25, 0x13, 0x11, 0x01, 0x93]
    assert p.from_addr == "13.25.80" and p.to_addr == "2B.78.11"
    assert p.cmd_name == "On" and p.crc_ok and p.valid


def test_build_padding():
    std = Packet.build("13.25.80", "16.3F.E5", cmd1=0x13)
    assert len(std.data) == 13 and std.data[-3:] == [0, 0, 0xAA]
    ext = Packet.build("13.25.80", "16.3F.E5", cmd1=0x2F, ext_data=[0, 0, 0x0F, 0xFF, 1])
    assert ext.extended and len(ext.data) == 32 and ext.data[24:] == [0xAA] * 8
    assert ext.ext_crc_ok and ext.crc_ok


def test_build_group_broadcast():
    p = Packet.build("13.25.80", group=1, cmd1=0x11, bcast=True)
    assert p.is_group_broadcast and p.group == 1 and p.to_addr == "13.25.80" and p.from_addr is None
    assert p.msg_type_name == "Group Broadcast"


def test_build_requires_one_target():
    with pytest.raises(ValueError):
        Packet.build("13.25.80", cmd1=0x11)
    with pytest.raises(ValueError):
        Packet.build("13.25.80", "16.3F.E5", group=1, cmd1=0x11)


def test_bytes_and_dict_roundtrip():
    for p in (Packet.build("13.25.80", "16.3F.E5", cmd1=0x13),
              Packet.build("2B.93.07", "29.4E.52", cmd1=0x2F, ext_data=[1, 2, 3])):
        assert Packet.parse(bytes(p)) == p
        d = p.to_dict()
        assert Packet.from_dict(d) == p
        assert json.loads(json.dumps(d))["raw"] == bytes(p).hex().upper()
        assert d["to"] == str(p.to_addr) and d["from"] == str(p.from_addr)
        assert d["crc_ok"] is True and d["msg_type"] == "DIRECT"
    p = Packet.build("13.25.80", group=7, cmd1=0x11, bcast=True)
    d = p.to_dict()
    assert d["group"] == 7 and d["from"] is None and d["msg_type"] == "GROUP_BROADCAST"
    with pytest.raises(ValueError):
        Packet.from_dict({})


def test_packet_equality_ignores_metadata():
    a = Packet.build("13.25.80", "16.3F.E5", cmd1=0x13)
    b = Packet(list(a.data), bits="0101", timestamp=123.0, complete=False)
    assert a == b and a != Packet.build("13.25.80", "16.3F.E5", cmd1=0x11)
    assert len(a) == len(a.data)


def test_from_wire_appends_crc():
    p = Packet.from_wire([0x0B, 0xE5, 0x3F, 0x16, 0x80, 0x25, 0x13, 0x11, 0xBF], crc=True, pad=True)
    assert p.data == [0x0B, 0xE5, 0x3F, 0x16, 0x80, 0x25, 0x13, 0x11, 0xBF, 0x5F, 0, 0, 0xAA]


@pytest.mark.parametrize("invert", [True, False])
def test_bits_roundtrip(invert):
    for src, dst, cmd, ext in [("13.25.80", "16.3F.E5", 0x13, None),
                               ("2B.93.07", "29.4E.52", 0x2F, [0, 0, 0x0F, 0xFF, 1])]:
        p = Packet.build(src, dst, cmd1=cmd, ext_data=ext)
        bits = p.to_bits(invert=invert)
        assert len(bits) % 8 == 0
        assert (START_HEADER_INV if invert else START_HEADER) in bits
        pkts = parse_bits(bits)
        assert len(pkts) == 1
        assert pkts[0].data == p.data and pkts[0].complete and pkts[0].valid


def test_frame_indexes():
    std = Packet.build("13.25.80", "16.3F.E5", cmd1=0x13)
    bits = std.to_bits(invert=False)
    _, idx = decode_frames(bits, bits.find(START_HEADER) + 5)
    assert idx == [31] + list(range(11, -1, -1))
    ext = Packet.build("13.25.80", "16.3F.E5", cmd1=0x2F, extended=True)
    bits = ext.to_bits(invert=False)
    _, idx = decode_frames(bits, bits.find(START_HEADER) + 5)
    assert idx == [31] + list(range(30, -1, -1))


def test_parse_ignores_leading_garbage_and_multiple_packets():
    a = Packet.build("13.25.80", "16.3F.E5", cmd1=0x11, cmd2=0xFF)
    b = Packet.build("16.3F.E5", "13.25.80", cmd1=0x11, cmd2=0xFF, ack=True)
    stream = "0110" * 7 + a.to_bits() + "1" * 13 + b.to_bits()
    pkts = parse_bits(stream)
    assert [p.data for p in pkts] == [a.data, b.data]


def test_parse_no_header():
    assert parse_bits("01" * 200) == []
    assert parse_bits("") == []


def test_sample_demod_fixture():
    """The regression fixture from the README (fsk2_demod output of Dat/*.dat)."""
    line = (DATA / "sample-demod.txt").read_text().strip()
    pkts = parse_bits(line)
    assert len(pkts) == 1
    p = pkts[0]
    assert p.summary() == "41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C"
    assert p.data == [0x41, 0x80, 0x25, 0x13, 0x11, 0x0D, 0x27, 0x11, 0x01, 0x8C, 0x00]
    assert p.from_addr == "27.0D.11" and p.to_addr == "13.25.80"
    assert p.msg_type_name == "Group Cleanup Direct" and p.crc_ok and not p.complete


def test_readme_expected_line_is_current():
    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text()
    assert "41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C" in readme


def test_rfcat_capture_fixture():
    """Bursts captured with a Yard Stick One while the PLM queried three devices."""
    pkts = []
    for line in (DATA / "rfcat-get-engine.txt").read_text().splitlines():
        pkts += [p for p in parse_bits(line) if p.calc_crc is not None]
    valid = [p for p in pkts if p.crc_ok]
    assert len(valid) >= 10
    queries = {(p.from_addr, p.to_addr) for p in valid if not p.ack}
    assert ("2B.93.07", "29.4E.52") in queries and ("2B.93.07", "25.09.42") in queries
    acks = [p for p in valid if p.ack]
    assert acks and all(p.cmd1 == 0x0D and p.cmd2 == 0x02 for p in acks)
    assert all(p.cmd_name == "Get Insteon Engine Version" for p in valid)


def test_ext_summary_width_and_crc_flag():
    p = Packet.build("13.25.80", "16.3F.E5", cmd1=0x2F, extended=True)
    assert p.summary().endswith(f"crc {p.calc_crc:02X}")
    bad = Packet(p.data[:23] + [p.data[23] ^ 1] + p.data[24:])
    assert " CRC " in bad.summary() and not bad.valid


def test_dump_frames_mentions_crc():
    p = Packet.build("13.25.80", "16.3F.E5", cmd1=0x13)
    out = dump_frames(p.to_bits())
    assert "crc OK" in out and "idx= 0" in out and "flags: Direct" in out
