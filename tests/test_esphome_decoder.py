"""The T-Embed's on-board decoder against the Python reference.

``esphome/components/insteon_rf/decoder.cpp`` is what the board runs to turn
a FIFO capture into contract-v1 messages (Doc/REARCHITECTURE.md, "T-Embed:
decode on the device"). It has no ESPHome dependency, so the host compiles
it with ``tools/insteon_rf_decode.cpp`` into a small CLI and these tests hold
it to the reference, :func:`insteonrf.listener.capture_messages`:

* every case in ``tests/data/contract-v1/*-vectors.jsonl`` (290 real T-Embed
  captures and 31 synthetic edge cases) must come out identical;
* so must a few thousand generated captures -- random packets, hop copies,
  bit errors, either polarity, headers at the start or not, and noise;
* the cross-capture fold window (``MessageFolder``, :class:`listener.Folder`
  with millisecond times) must release the same messages as the reference
  over the real capture sequence in ``embed-captures.jsonl``.

Skipped when there is no ``g++``.
"""

from __future__ import annotations

import base64
import json
import pathlib
import random
import shutil
import subprocess
from typing import Any

import pytest

from insteonrf.listener import Folder, ListenerMessage, capture_messages
from insteonrf.monitor import packet_air_times
from insteonrf.packet import START_HEADER, START_HEADER_INV, Packet, parse_bits
from insteonrf.radio.mqtt import bits_from_bytes, bytes_from_bits, with_sync_header

REPO = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = REPO / "esphome" / "components" / "insteon_rf"
SOURCES = [REPO / "tools" / "insteon_rf_decode.cpp", COMPONENT / "decoder.cpp"]
VECTORS = REPO / "tests" / "data" / "contract-v1"
BAUD = 9124


@pytest.fixture(scope="session")
def decoder_cli(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    cxx = shutil.which("g++")
    if cxx is None:
        pytest.skip("no g++ to build the ESPHome decoder with")
    exe = tmp_path_factory.mktemp("decoder") / "insteon_rf_decode"
    subprocess.run([cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(exe),
                    *map(str, SOURCES)], check=True, capture_output=True, text=True)
    return exe


def _run(exe: pathlib.Path, lines: list[str], *args: str) -> list[Any]:
    out = subprocess.run([str(exe), *args], input="".join(f"{s}\n" for s in lines),
                         capture_output=True, text=True, check=True).stdout
    return [json.loads(s) for s in out.splitlines()]


def _reference(bits: str, sync_word: int) -> list[dict[str, Any]]:
    return [{"frame": m.frame.hex().upper(), "copies": m.copies, "hops_left": m.hops_left}
            for m in capture_messages(bits, "x", 0.0, sync_word=sync_word)]


@pytest.mark.parametrize("name", ["embed-vectors.jsonl", "synthetic-vectors.jsonl"])
def test_contract_vectors(decoder_cli: pathlib.Path, name: str) -> None:
    cases = [json.loads(s) for s in (VECTORS / name).read_text().splitlines() if s.strip()]
    assert cases
    got = _run(decoder_cli, [f"{c['capture']['sw']} {c['capture']['b']}" for c in cases])
    assert len(got) == len(cases)
    bad = [(c.get("name", c["capture"].get("seq")), c["expected"], g)
           for c, g in zip(cases, got, strict=True) if g != c["expected"]]
    assert not bad, f"{len(cases) - len(bad)}/{len(cases)} identical; first mismatches: {bad[:3]}"


# --------------------------------------------------------------------- generated

PLM = "2B.93.07"
DEVICES = ["29.4E.52", "3F.4E.D6", "18.8F.FB", "2B.A0.AB", "16.3F.E5"]


def _random_packet(rng: random.Random) -> Packet:
    max_hops = rng.randint(0, 3)
    hops = rng.randint(0, max_hops)
    if rng.random() < 0.2:
        return Packet.build(rng.choice(DEVICES), group=rng.randint(0, 255), cmd1=rng.randint(0, 255),
                            cmd2=rng.randint(0, 255), bcast=True, max_hops=max_hops, hops_left=hops)
    ext = [rng.randint(0, 255) for _ in range(13)] if rng.random() < 0.25 else None
    return Packet.build(rng.choice(DEVICES), rng.choice(DEVICES + [PLM]), cmd1=rng.randint(0, 255),
                        cmd2=rng.randint(0, 255), ext_data=ext, ack=rng.random() < 0.3,
                        max_hops=max_hops, hops_left=hops,
                        crc=rng.randint(0, 255) if rng.random() < 0.05 else None)


def _random_capture(rng: random.Random) -> tuple[str, int]:
    """A FIFO dump (bits after the sync word) and the sync word, like the board's."""
    kind = rng.random()
    if kind < 0.1:
        bits = "".join(rng.choice("01") for _ in range(rng.randint(1, 160) * 8))
    else:
        pkt = _random_packet(rng)
        copies = []
        for hop in range(pkt.hops_left, max(-1, pkt.hops_left - rng.randint(1, 3)), -1):
            c = Packet(list(pkt.data))
            c.data[0] = (c.data[0] & ~0x0C) | (hop << 2)
            c.data[c.crc_index] = (Packet.from_wire(c.data[:c.crc_index], crc=True).data[c.crc_index]
                                   if rng.random() > 0.05 else rng.randint(0, 255))
            copies.append(c)
        if rng.random() < 0.3:
            copies.append(_random_packet(rng))
        on_air = "".join(c.to_bits() + "01" * rng.randint(0, 40) for c in copies)
        # The radio swallows the preamble and the start header it synced on.
        start = on_air.find(START_HEADER_INV)
        bits = on_air[start + 16:] if rng.random() < 0.85 else on_air[rng.randint(0, 40):]
        flips = rng.choice([0, 0, 0, 1, 2, 5])
        b = list(bits)
        for _ in range(flips):
            i = rng.randrange(len(b))
            b[i] = "1" if b[i] == "0" else "0"
        bits = "".join(b)
        if rng.random() < 0.05:
            bits = (START_HEADER if rng.random() < 0.5 else START_HEADER_INV) + bits
        if rng.random() < 0.3:
            bits = bits[: rng.randint(17, len(bits))]
    sync_word = 0x3155
    if rng.random() < 0.15:
        sync_word = 0xCEAA
        bits = bits.translate(str.maketrans("01", "10"))
    bits = bits[: 255 * 8]
    return bits_from_bytes(bytes_from_bits(bits)), sync_word


def test_generated_captures_match_reference(decoder_cli: pathlib.Path) -> None:
    rng = random.Random(20261115)
    caps = [_random_capture(rng) for _ in range(3000)]
    got = _run(decoder_cli, [f"{sw:04X} {base64.b64encode(bytes_from_bits(bits)).decode()}"
                             for bits, sw in caps])
    want = [_reference(with_sync_header(bits, sw), sw) for bits, sw in caps]
    assert sum(1 for w in want if w) > 1500, "the generator should mostly produce decodable captures"
    bad = [(i, w, g) for i, (w, g) in enumerate(zip(want, got, strict=True)) if w != g]
    assert not bad, f"{len(caps) - len(bad)}/{len(caps)} identical; first: {bad[:3]}"


# --------------------------------------------------------------------- fold window


def _folded_reference(caps: list[dict[str, Any]]) -> list[ListenerMessage]:
    """listener.Folder over a capture sequence, dated per copy, released on the
    captures' own clock (what the firmware does with the wall clock)."""
    folder = Folder("x")
    out: list[ListenerMessage] = []
    for c in caps:
        t = c["t"] / 1000.0
        out += folder.pop_ready(now=t)
        bits = with_sync_header(bits_from_bytes(base64.b64decode(c["b"])), int(c["sw"], 16))
        pkts = parse_bits(bits)
        for p, when in zip(pkts, packet_air_times(bits, pkts, t, BAUD), strict=True):
            p.rssi_dbm = c.get("rssi")
            folder.add(p, t=when)
    return out + folder.flush()


def test_cross_capture_fold_matches_reference(decoder_cli: pathlib.Path) -> None:
    caps = [json.loads(s) for s in (VECTORS / "embed-captures.jsonl").read_text().splitlines() if s.strip()]
    got = _run(decoder_cli, [f"{c['t']} {c.get('rssi', 'null')} {c['sw']} {c['b']}" for c in caps], "--fold")
    want = _folded_reference(caps)
    assert len(want) > 100
    key = lambda d: (d["t_ms"], d["frame"])  # noqa: E731
    want_d = sorted(({"t_ms": round(m.t * 1000), "frame": m.frame.hex().upper(), "copies": m.copies,
                      "hops_left": m.hops_left, "rssi": m.rssi} for m in want), key=key)
    got = sorted(got, key=key)
    assert len(got) == len(want_d)
    for g, w in zip(got, want_d, strict=True):
        assert abs(g["t_ms"] - w["t_ms"]) <= 1, (g, w)
        assert {k: g[k] for k in ("frame", "copies", "hops_left", "rssi")} == \
            {k: w[k] for k in ("frame", "copies", "hops_left", "rssi")}


def test_fold_joins_a_transmission_split_across_captures(decoder_cli: pathlib.Path) -> None:
    """Two captures 100 ms apart holding hops 3 and 2 of one message are one
    message; the same bytes 0.7 s later are a retry, i.e. a second one."""
    def fifo(hops: int) -> str:
        p = Packet.build("29.4E.52", PLM, cmd1=0x11, cmd2=0xFF, max_hops=3, hops_left=hops)
        bits = p.to_bits()
        bits = bits[bits.find(START_HEADER_INV) + 16:]
        return base64.b64encode(bytes_from_bits(bits)).decode()

    got = _run(decoder_cli, [f"1000 -80.0 3155 {fifo(3)}", f"1100 -70.5 3155 {fifo(2)}",
                             f"1800 -90.0 3155 {fifo(3)}"], "--fold")
    assert [(g["t_ms"], g["copies"], g["hops_left"], g["rssi"]) for g in got] == \
        [(1000, 2, 3, -70.5), (1800, 1, 3, -90.0)]
    assert got[0]["frame"] == "0250294E522B93070F11FF"
