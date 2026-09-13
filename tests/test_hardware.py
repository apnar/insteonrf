"""Tests that need the real radio. Skipped unless INSTEONRF_HW=1.

    INSTEONRF_HW=1 pytest tests/test_hardware.py -v

Only benign commands are ever transmitted (Ping 0x0F), and only from the PLM
address, as PLAN.md requires.
"""

import time

import pytest

from insteonrf.packet import Packet, parse_bits
from insteonrf.radio import RfcatRadio

pytestmark = pytest.mark.hardware

PLM = "2B.93.07"
TARGET = "29.4E.52"  # Master Bedroom KeypadLinc


def test_open_configure_close():
    with RfcatRadio() as radio:
        radio.configure_rx()
        assert radio.dev.getBuildInfo()
        assert radio.usb_errors >= 0
    # After close the interface must be free for an immediate second open.
    with RfcatRadio() as radio:
        radio.configure_rx()


def test_receive_survives_a_reopen():
    """The watchdog path: heal() must come back with the same RX config."""
    with RfcatRadio() as radio:
        radio.configure_rx(sync_header=True)
        radio.receive_bits(500)
        radio.heal()
        assert radio._rx_kwargs == {"sync_header": True, "blocksize": 255}
        radio.receive_bits(500)


def test_ping_is_acked():
    """Spoof the PLM to Ping a device and decode its ACK."""
    pkt = Packet.build(PLM, TARGET, cmd1=0x0F, cmd2=0x00)
    acks = []
    with RfcatRadio() as radio:
        for _ in range(3):
            radio.configure_tx()
            radio.transmit_bits(pkt.to_bits())
            radio.configure_rx(sync_header=True)
            end = time.monotonic() + 1.5
            while (left := end - time.monotonic()) > 0:
                got = radio.receive_bits(max(1, int(left * 1000)))
                if got is None:
                    continue
                for q in parse_bits(got[1], got[0]):
                    if q.crc_ok and q.ack and q.from_addr == TARGET:
                        acks.append(q)
            if acks:
                break
    assert acks, "no ACK heard — is the device powered and linked to the PLM?"
    assert acks[0].to_addr == PLM and acks[0].cmd1 == 0x0F
