"""The ESPHome listener boards, checked from the host side.

The firmware cannot run here, but the assumptions it is built on can: what a
radio's FIFO holds after its sync word, where the Manchester gate expects
pairs and frame markers in it, how a 16-bit-sync capture (the CC1101 on the
T-Embed) is put back together by the host, and that the T-Embed's pin map
has not drifted from LilyGO's.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import time

import pytest

from insteonrf.fusion import sightings_from_capture
from insteonrf.packet import START_HEADER_INV, Packet
from insteonrf.radio.mqtt import Capture, bits_from_bytes, bytes_from_bits

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "esphome" / "components" / "insteon_rf"
EMBED_YAML = ROOT / "esphome" / "insteon-rf-embed.yaml"

PLM = "2B.93.07"
DEV = "29.4E.52"
#: Insteon RF slot pitch: consecutive hops start this many bits apart.
SLOT_BITS = 456
#: The default capture: three whole slots, ending short of slot 3's sync.
CAPTURE_BYTES = 162


def after_header(p: Packet) -> str:
    """The on-air bits that follow the start header -- what the FIFO holds."""
    b = p.to_bits()
    i = b.find(START_HEADER_INV)
    assert i >= 0
    return b[i + len(START_HEADER_INV):]


def slot_capture(*packets: Packet, nbytes: int = CAPTURE_BYTES) -> bytes:
    """A fixed-length capture: packets on consecutive slots, as on the air.

    The first packet's header was consumed by the sync word; later ones keep
    theirs, starting a whole slot after the one before. The gaps are idle
    air, which a hardware demodulator turns into noise; zeros stand in.
    """
    first = packets[0].to_bits().find(START_HEADER_INV) + len(START_HEADER_INV)
    stream = ["0"] * (nbytes * 8)
    for k, p in enumerate(packets):
        b = p.to_bits()
        start = b.find(START_HEADER_INV)
        body = b[first:] if k == 0 else b[start:]
        # The k-th packet's header starts a whole slot after the first's.
        at = 0 if k == 0 else k * SLOT_BITS - (first - start)
        for i, c in enumerate(body):
            if at + i < len(stream):
                stream[at + i] = c
    return bytes_from_bits("".join(stream))


def payload(raw: bytes, sw: str, name: str = "insteon-rf-embed") -> bytes:
    return json.dumps({
        "n": name, "seq": 1, "t": int(time.time() * 1000), "us": 1, "rssi": -90.0,
        "len": len(raw), "sw": sw, "b": base64.b64encode(raw).decode(),
    }).encode()


# --------------------------------------------------------------------------- the gate


def gate_ok(buf: bytes, frames: int = 4, errors: int = 2) -> bool:
    """A transliteration of ``InsteonRF::manchester_gate_ok_`` (insteon_rf.cpp).

    Kept literal so the layout it assumes -- data pairs from bit 1, frame
    markers at 17 + 28k -- is tested against ``Packet.to_bits()``, which is
    validated against real devices.
    """
    bits = bits_from_bytes(buf)
    if len(bits) < 17:
        return False
    bad = 0
    for j in range(8):
        at = 1 + 2 * j
        if bits[at] == bits[at + 1]:
            bad += 1
            if bad > errors:
                return False
    for k in range(frames - 1):
        base = 17 + k * 28
        if base + 28 > len(bits):
            break
        if bits[base] != bits[base + 1]:
            bad += 1
            if bad > errors:
                return False
        for j in range(13):
            at = base + 2 + 2 * j
            if bits[at] == bits[at + 1]:
                bad += 1
                if bad > errors:
                    return False
    return True


def test_the_transliteration_still_matches_the_firmware():
    """If the C++ layout constants move, this mirror is stale."""
    src = (COMPONENT / "insteon_rf.cpp").read_text()
    for needle in ("const size_t at = 1 + 2 * j;", "17 + (size_t) k * 28",
                   "base + 2 + 2 * j", "j < 13"):
        assert needle in src, needle


@pytest.mark.parametrize("pkt", [
    Packet.build(PLM, DEV, cmd1=0x0D, cmd2=0x00),
    Packet.build(DEV, group=1, bcast=True, cmd1=0x11, cmd2=0xFF),
    Packet.build(PLM, DEV, cmd1=0x2F, cmd2=0x00, ext_data=range(13)),
], ids=["direct", "broadcast", "extended"])
def test_a_real_packet_passes_the_gate_strictly(pkt):
    """Every gated frame of a clean packet is valid -- with no slack at all."""
    raw = slot_capture(pkt)
    assert gate_ok(raw, frames=13 if pkt.extended else 4, errors=0)


def test_noise_fails_the_gate():
    import random

    rng = random.Random(1234)
    passed = sum(gate_ok(bytes(rng.getrandbits(8) for _ in range(CAPTURE_BYTES)))
                 for _ in range(2000))
    assert passed == 0


def test_a_header_one_bit_late_fails_the_gate():
    """The gate is also an alignment check: a capture that started one bit
    off (a 15/16 sync that matched shifted) has its pairs straddled."""
    bits = after_header(Packet.build(PLM, DEV, cmd1=0x0D, cmd2=0x00))
    assert not gate_ok(bytes_from_bits("0" + bits), errors=0)


# --------------------------------------------------------------------------- CC1101 captures


def test_a_cc1101_capture_decodes_with_its_16_bit_sync_word():
    """The CC1101 reports sw 0x3155 -- the low half of the Heltec's word --
    and the host must put back the same start header."""
    pkt = Packet.build(PLM, DEV, cmd1=0x0D, cmd2=0x00)
    cap = Capture.from_payload("insteon-rf/rx/insteon-rf-embed",
                               payload(slot_capture(pkt), sw="00003155"))
    assert cap is not None and cap.sync_word == 0x3155
    assert cap.bits.startswith(START_HEADER_INV)
    got = sightings_from_capture(cap.bits, cap.receiver)
    assert got and got[0].packet.crc_ok is True
    assert str(got[0].packet.to_addr) == DEV and got[0].packet.cmd1 == 0x0D


def test_a_full_capture_holds_three_slots():
    """162 bytes is sized to the slot grid: a message, its hop repeat and the
    ACK in slot 2 all decode out of one capture."""
    msg = Packet.build(PLM, DEV, cmd1=0x0D, cmd2=0x00, max_hops=1, hops_left=1)
    hop = Packet.build(PLM, DEV, cmd1=0x0D, cmd2=0x00, max_hops=1, hops_left=0)
    ack = Packet.build(DEV, PLM, cmd1=0x0D, cmd2=0x02, ack=True)
    cap = Capture.from_payload("insteon-rf/rx/insteon-rf-embed",
                               payload(slot_capture(msg, hop, ack), sw="00003155"))
    got = [s.packet for s in sightings_from_capture(cap.bits, cap.receiver)]
    assert len(got) == 3 and all(p.crc_ok for p in got)
    assert [p.hops_left for p in got[:2]] == [1, 0]
    assert str(got[2].from_addr) == DEV


@pytest.mark.parametrize("sync_bits", [16, 32], ids=["cc1101", "sx1262"])
def test_the_capture_ends_before_slot_3s_sync(sync_bits):
    """Ending inside slot 3's sync word would cost that slot's packet: the
    chip only resumes hunting once the capture is complete. FIFO bit 0 is the
    bit after slot 0's start header, so slot k's header begins at
    k * 456 - 16, and a sync word that long reaches back from its end."""
    slot3_sync_starts = 3 * SLOT_BITS - len(START_HEADER_INV) - (sync_bits - 16)
    assert CAPTURE_BYTES * 8 <= slot3_sync_starts
    # ...and it still holds slot 2 whole: a packet is 364 bits.
    assert CAPTURE_BYTES * 8 >= 2 * SLOT_BITS - len(START_HEADER_INV) + 364 - 16


# --------------------------------------------------------------------------- T-Embed pin map


def _embed() -> str:
    return EMBED_YAML.read_text(encoding="utf-8")


def _block(key: str) -> str:
    m = re.search(rf"^{key}:\n((?:[ \t].*\n|\n)+)", _embed(), re.MULTILINE)
    assert m, f"no top-level {key}: block"
    return m.group(1)


def test_the_embed_radio_pins_match_lilygo():
    """LilyGO examples/utilities.h: CC1101 CS 12, GDO0 3, GDO2 38, band
    switch SW1 47 / SW0 48, shared SPI 11/9/10."""
    rf = _block("insteon_rf")
    for key, pin in (("cs_pin", 12), ("gdo0_pin", 3), ("gdo2_pin", 38),
                     ("sw0_pin", 48), ("sw1_pin", 47)):
        assert re.search(rf"^\s+{key}:(?: |\n\s+number: )GPIO{pin}\s*$", rf, re.MULTILINE), key
    assert re.search(r"^\s+radio: cc1101\s*$", rf, re.MULTILINE)
    spi = _block("spi")
    for key, pin in (("clk_pin", 11), ("mosi_pin", 9), ("miso_pin", 10)):
        assert re.search(rf"^\s+{key}: GPIO{pin}\s*$", spi, re.MULTILINE), key


@pytest.mark.parametrize("pin,mode", [
    (15, "ALWAYS_ON"),   # BOARD_PWR_EN: the CC1101's supply
    (13, "ALWAYS_ON"),   # SD CS held inactive
    (44, "ALWAYS_ON"),   # nRF24 CS held inactive (Plus)
    (43, "ALWAYS_OFF"),  # nRF24 CE: standby
])
def test_the_embed_holds_the_shared_bus_quiet(pin, mode):
    switches = _block("switch")
    m = re.search(rf"pin: GPIO{pin}\n\s+restore_mode: (\w+)", switches)
    assert m and m.group(1) == mode


def test_the_embed_keeps_uart0_free_for_the_nrf24():
    assert re.search(r"hardware_uart: USB_SERIAL_JTAG", _block("logger"))


def test_the_embed_publishes_where_the_mesh_listens():
    rf = _block("insteon_rf")
    assert re.search(r"mqtt_topic: insteon-rf/rx/\$\{name\}", rf)
