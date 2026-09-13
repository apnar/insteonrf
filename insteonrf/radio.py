"""rfcat (TI CC1111) radio front end for Insteon RF.

Insteon RF is 2-FSK at 915 MHz, ~9.12 kbaud, with the packet framing
described in :mod:`insteonrf.packet`. The dongle does the FSK demodulation;
this module only configures it and shuffles bits in and out.

Requires the ``rflib`` package from https://github.com/atlas0fd00m/rfcat
and a CC1111 device running the rfcat firmware (e.g. a Yard Stick One).
"""

from __future__ import annotations

import time
from typing import Iterator

from .manchester import invert_bits
from .packet import START_HEADER_INV

DEFAULT_FREQ = 914_950_000
DEFAULT_DRATE = 9124
CHANNEL_BW = 200_000
DEVIATION = 75_000

#: On-air Insteon preamble pattern (inverted Manchester of ``0101...``).
SYNC_PREAMBLE = 0x6666
#: On-air (inverted) start header, ``START_HEADER_INV`` as a 16-bit word.
SYNC_START_HEADER = int(START_HEADER_INV, 2)


def _rflib():
    try:
        import rflib
    except ImportError as err:  # pragma: no cover - depends on environment
        raise SystemExit(
            "rflib not found: install rfcat (pip install git+https://github.com/atlas0fd00m/rfcat)"
        ) from err
    return rflib


class Radio:
    """A configured rfcat dongle. Use as a context manager to release USB cleanly."""

    def __init__(self, freq: int = DEFAULT_FREQ, drate: int = DEFAULT_DRATE, *,
                 index: int = 0, debug: bool = False):
        self.rflib = rf = _rflib()
        self.freq = freq
        self.drate = drate
        self.dev = rf.RfCat(idx=index, debug=debug)
        self._sync_header = False
        self._common()

    # ---- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Radio":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.dev.setModeIDLE()
        finally:
            # Without an explicit cleanup, libusb teardown at interpreter exit
            # can segfault (seen with pyusb on Python 3.14).
            self.dev.cleanup()

    # ---- configuration ---------------------------------------------------

    def _common(self) -> None:
        d, rf = self.dev, self.rflib
        d.setModeIDLE()
        d.setFreq(self.freq)
        d.setMdmModulation(rf.MOD_2FSK)
        d.setMdmDRate(self.drate)
        d.setMdmChanBW(CHANNEL_BW)
        d.setMdmDeviatn(DEVIATION)
        d.setEnableMdmManchester(False)
        d.setEnableMdmFEC(False)
        d.setEnablePktCRC(False)
        d.setEnablePktDataWhitening(False)
        d.setEnablePktAppendStatus(False)

    def configure_rx(self, *, sync_header: bool = True, blocksize: int = 255) -> None:
        """Set up for receiving.

        By default the CC1111 triggers on the Insteon start header and
        delivers ``blocksize``-byte blocks (the header is re-added to each
        block so the output stays parseable). With ``sync_header=False`` it
        captures on carrier detect alone, which is noisier but also shows
        bursts whose header the sync detector missed.
        :func:`insteonrf.packet.parse_bits` finds the packets inside either.
        """
        d, rf = self.dev, self.rflib
        d.setModeIDLE()
        d.setBSLimit(rf.BSCFG_BS_LIMIT_6)
        d.makePktFLEN(blocksize)
        d.setPktPQT(0)
        d.setMdmNumPreamble(rf.MFMCFG1_NUM_PREAMBLE_4)
        self._sync_header = sync_header
        if sync_header:
            d.setMdmSyncWord(SYNC_START_HEADER)
            d.setMdmSyncMode(rf.SYNCM_CARRIER_16_of_16)
        else:
            d.setMdmSyncWord(SYNC_PREAMBLE)
            d.setMdmSyncMode(rf.SYNCM_CARRIER)
        d.setModeRX()

    def configure_tx(self) -> None:
        """Set up for transmitting: the radio adds its own ``AA`` preamble and a
        ``6666`` sync word that continues the Insteon preamble pattern; the
        packet bits from :meth:`Packet.to_bits` follow."""
        d, rf = self.dev, self.rflib
        d.setModeIDLE()
        d.setBSLimit(rf.BSCFG_BS_LIMIT_12)
        d.setMdmNumPreamble(rf.MFMCFG1_NUM_PREAMBLE_4)
        d.setMdmSyncWord(SYNC_PREAMBLE)
        d.setMdmSyncMode(rf.SYNCM_16_of_16)
        d.setMaxPower()

    def print_config(self) -> None:
        self.dev.printRadioConfig()

    # ---- I/O -------------------------------------------------------------

    def receive(self, timeout_ms: int = 2000) -> tuple[float, bytes] | None:
        """Return ``(timestamp, raw bytes)`` for the next block, or None on timeout."""
        try:
            data, ts = self.dev.RFrecv(timeout=timeout_ms)
        except self.rflib.ChipconUsbTimeoutException:
            return None
        return ts, bytes(data)

    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
        """Like :meth:`receive` but returns an ASCII bit string in on-air polarity."""
        r = self.receive(timeout_ms)
        if r is None:
            return None
        ts, data = r
        bits = "".join(f"{b:08b}" for b in data)
        if self._sync_header:
            bits = START_HEADER_INV + bits
        return ts, bits

    def iter_bits(self, timeout_ms: int = 2000) -> Iterator[tuple[float, str]]:
        """Yield received bit strings forever (until the caller stops iterating)."""
        while True:
            r = self.receive_bits(timeout_ms)
            if r is not None:
                yield r

    def transmit_bits(self, bits: str, *, repeat: int = 1, gap_s: float = 0.06,
                      invert: bool = False) -> None:
        """Transmit an ASCII bit string (as produced by ``Packet.to_bits()``).

        The string is padded to a byte boundary. ``repeat`` sends it that many
        times, ``gap_s`` apart.
        """
        if invert:
            bits = invert_bits(bits)
        if len(bits) % 8:
            bits += ("01" * 4)[: 8 - len(bits) % 8]
        data = bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))
        if len(data) > 255:
            raise ValueError(f"packet too long for one rfcat block: {len(data)} bytes")
        d = self.dev
        d.makePktFLEN(len(data))
        for n in range(repeat):
            if n:
                time.sleep(gap_s)
            d.RFxmit(data)
        d.setModeIDLE()
