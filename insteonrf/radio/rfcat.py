"""rfcat (TI CC1111) radio front end for Insteon RF.

Insteon RF is 2-FSK at 915 MHz, ~9.12 kbaud, with the packet framing
described in :mod:`insteonrf.packet`. The dongle does the FSK demodulation;
this module only configures it and shuffles bits in and out.

Requires the ``rflib`` package from https://github.com/atlas0fd00m/rfcat
and a CC1111 device running the rfcat firmware (e.g. a Yard Stick One).

**Robustness.** A CC1111 occasionally stops answering bulk endpoint 5 — every
``RFrecv`` then times out while rflib's receive thread prints
``Error in resetup():USBTimeoutError`` once a second, forever, because
``USBDongle.resetup()`` retries without a deadline. This module bounds that
loop (:class:`_BoundedRfCat`), silences it, and can USB-reset and reopen the
dongle when it happens (``auto_reset``, on by default).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

from ..manchester import invert_bits
from ..packet import START_HEADER_INV

log = logging.getLogger(__name__)

DEFAULT_FREQ = 914_950_000
DEFAULT_DRATE = 9124
CHANNEL_BW = 200_000
DEVIATION = 75_000

#: USB ids of an rfcat dongle (a Yard Stick One / Don's Dongle).
USB_VID = 0x1D50
USB_PID = 0x6048

#: On-air Insteon preamble pattern (inverted Manchester of ``0101...``).
SYNC_PREAMBLE = 0x6666
#: On-air (inverted) start header, ``START_HEADER_INV`` as a 16-bit word.
SYNC_START_HEADER = int(START_HEADER_INV, 2)

#: CC1101/CC1111 RSSI register offset for this configuration (datasheet).
RSSI_OFFSET_DB = 74.0

#: How long to wait for the dongle to come back before giving up on it.
OPEN_TIMEOUT = 5.0
RESETUP_TIMEOUT = 8.0
RESET_SETTLE = 3.0


class DongleError(RuntimeError):
    """The dongle is not responding (and a USB reset did not fix it)."""


def _rflib() -> Any:
    try:
        import rflib
    except ImportError as err:  # pragma: no cover - depends on environment
        raise SystemExit(
            "rflib not found: install rfcat (pip install git+https://github.com/atlas0fd00m/rfcat)"
        ) from err
    return rflib


def usb_reset(vid: int = USB_VID, pid: int = USB_PID, timeout: float = 10.0) -> bool:
    """USB-reset the dongle. Returns True if the reset call completed.

    ``pyusb``'s ``reset()`` can itself block for tens of seconds on a wedged
    device and takes no timeout, so it runs in a daemon thread that we simply
    abandon if it overruns — the caller then knows to ask for a replug.
    """
    import usb.core

    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        log.warning("no USB device %04x:%04x found", vid, pid)
        return False

    done = threading.Event()

    def _do_reset() -> None:
        try:
            dev.reset()
        except Exception as err:  # a timeout here is common; it usually works anyway
            log.debug("usb reset raised: %r", err)
        finally:
            done.set()

    t = threading.Thread(target=_do_reset, daemon=True, name="usb-reset")
    t.start()
    if not done.wait(timeout):
        log.error("USB reset did not finish in %.0fs — unplug and replug the dongle", timeout)
        return False
    return True


def _bounded_rfcat_class() -> Any:
    """Build the ``RfCat`` subclass with a bounded, quiet ``resetup()``."""
    rf = _rflib()

    class _BoundedRfCat(rf.RfCat):  # type: ignore[misc,name-defined]
        resetup_timeout = RESETUP_TIMEOUT

        def resetup(self, console: bool = True, copyDongle: Any = None) -> None:
            # Same job as USBDongle.resetup(), but it gives up instead of
            # retrying forever, and it does not print a line per attempt.
            self._quiet = True
            if getattr(self, "_closed", False):
                # Closed from under rflib's ctrl thread (which calls resetup()
                # whenever reset_event fires): do not re-claim the interface.
                return
            self._do = None
            self.resetup_error: BaseException | None = None
            if self._bootloader:
                return
            deadline = time.monotonic() + self.resetup_timeout
            while self._do is None:
                try:
                    self.setup(console, copyDongle)
                    if copyDongle is None:
                        self._clear_buffers(False)
                    if not self._safemode:
                        self.ping(3, wait=10, silent=True)
                        self.setRfMode(self._rfmode)
                except Exception as err:
                    if time.monotonic() >= deadline:
                        self.resetup_error = err
                        log.debug("resetup gave up: %r", err)
                        if threading.current_thread() is threading.main_thread():
                            raise DongleError(f"dongle did not come up: {err!r}") from err
                        return  # background thread: the watchdog will reopen
                    time.sleep(0.5)

    return _BoundedRfCat


def _call_with_timeout(fn: Any, timeout: float) -> tuple[bool, Any]:
    """Run ``fn()`` in a daemon thread; return ``(finished, result_or_exception)``."""
    box: list[Any] = []
    done = threading.Event()

    def run() -> None:
        try:
            box.append(fn())
        except BaseException as err:  # noqa: BLE001 - reported to the caller
            box.append(err)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="rfcat-open").start()
    if not done.wait(timeout):
        return False, None
    return True, box[0] if box else None


#: Long enough for rflib's receive thread to come out of its ``bulkRead``
#: (``EP_TIMEOUT_ACTIVE`` is 10 ms, the idle timeout 400 ms).
THREAD_SETTLE = 0.5


def _release(dev: Any) -> None:
    """Idle a dongle, stop rflib's worker threads and hand the interface back.

    ``USBDongle.cleanup()`` only resets rflib's queues: it leaves the three
    daemon threads running and the USB interface claimed. A second ``RfCat()``
    in the same process then fails with ``USBError(16, 'Resource busy')``, and
    a process that exits without releasing leaves the interface claimed until
    the kernel tears the file descriptor down — which is how the dongle ends up
    looking wedged to the next run.
    """
    if dev is None:
        return
    dev._closed = True  # stop rflib's ctrl thread from reopening behind us
    try:
        dev.setModeIDLE()
    except Exception as err:  # a wedged dongle cannot be idled; carry on
        log.debug("setModeIDLE on close: %r", err)
    # Stop the ctrl/recv/send threads before touching the interface.
    try:
        dev._threadGo.clear()
        dev.reset_event.clear()
        time.sleep(THREAD_SETTLE)
    except Exception as err:
        log.debug("stopping rflib threads: %r", err)
    handle = getattr(dev, "_do", None)
    for call in ("releaseInterface", "finalize"):
        fn = getattr(handle, call, None)
        if fn is None:
            continue
        try:
            fn()
        except Exception as err:
            log.debug("%s on close: %r", call, err)
    dev._do = None
    try:
        # Without an explicit cleanup, libusb teardown at interpreter exit
        # can segfault (seen with pyusb on Python 3.14).
        dev.cleanup()
    except Exception as err:
        log.debug("cleanup on close: %r", err)


class RfcatRadio:
    """A configured rfcat dongle. Use as a context manager to release USB cleanly."""

    name = "rfcat"
    #: A live radio never runs out of input; ``receive_bits() is None`` is a timeout.
    exhausted = False

    def __init__(self, freq: int = DEFAULT_FREQ, drate: int = DEFAULT_DRATE, *,
                 index: int = 0, debug: bool = False, auto_reset: bool = True,
                 max_usb_errors: int = 3):
        self.rflib = _rflib()
        self.freq = freq
        self.drate = drate
        self.index = index
        self.debug = debug
        self.auto_reset = auto_reset
        self.max_usb_errors = max_usb_errors
        self._sync_header = False
        self._rx_kwargs: dict[str, Any] | None = None
        self._tx = False
        self._timeouts = 0
        self._heals = 0
        self.dev = self._open()
        self._common()

    # ---- lifecycle -------------------------------------------------------

    def _open(self) -> Any:
        """Open the dongle, USB-resetting it once if it does not answer."""
        cls = _bounded_rfcat_class()
        for attempt in (1, 2):
            ok, res = _call_with_timeout(lambda: cls(idx=self.index, debug=self.debug),
                                         OPEN_TIMEOUT + RESETUP_TIMEOUT)
            if ok and not isinstance(res, BaseException):
                dev = res
                dev._quiet = True
                if self._alive(dev):
                    return dev
                log.warning("dongle opened but is not answering%s",
                            f": {getattr(dev, 'resetup_error', None)!r}"
                            if getattr(dev, "resetup_error", None) else "")
                _release(dev)
            elif not ok:
                log.warning("dongle did not open within %.0fs", OPEN_TIMEOUT + RESETUP_TIMEOUT)
            else:
                log.warning("dongle would not open: %r", res)
            if attempt == 2 or not self.auto_reset:
                break
            log.warning("dongle not responding, resetting USB…")
            usb_reset()
            time.sleep(RESET_SETTLE)
        raise DongleError(
            "rfcat dongle is not responding. Try 'insteon-rf reset', wait a few "
            "seconds, and if that does not help, unplug and replug it."
        )

    @staticmethod
    def _alive(dev: Any, timeout: float = OPEN_TIMEOUT) -> bool:
        ok, res = _call_with_timeout(dev.getBuildInfo, timeout)
        return ok and not isinstance(res, BaseException)

    def __enter__(self) -> RfcatRadio:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        dev, self.dev = getattr(self, "dev", None), None
        _release(dev)

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
        self._rx_kwargs = {"sync_header": sync_header, "blocksize": blocksize}
        self._tx = False
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
        self._tx = True
        self._rx_kwargs = None
        d, rf = self.dev, self.rflib
        d.setModeIDLE()
        d.setBSLimit(rf.BSCFG_BS_LIMIT_12)
        d.setMdmNumPreamble(rf.MFMCFG1_NUM_PREAMBLE_4)
        d.setMdmSyncWord(SYNC_PREAMBLE)
        d.setMdmSyncMode(rf.SYNCM_16_of_16)
        d.setMaxPower()

    def print_config(self) -> None:
        self.dev.printRadioConfig()

    # ---- signal strength ------------------------------------------------

    def read_rssi(self) -> float | None:
        """Current RSSI in dBm, or None if the dongle will not answer.

        This is the CC1111's RSSI register read *after* a block arrived, not a
        per-packet measurement latched with it — the radio is still in RX, so
        it reflects the channel a moment later. Good enough to watch a link
        degrade over days; do not read it as a calibrated packet RSSI.

        The register is a signed half-dB value with a fixed offset (74 dB for
        this configuration, per the CC1101/CC1111 datasheet).
        """
        dev = self.dev
        if dev is None:
            return None
        try:
            raw = int(dev.getRSSI()[0] if isinstance(dev.getRSSI(), (bytes, bytearray))
                      else dev.getRSSI())
        except Exception as err:  # a wedged dongle, or an older rflib
            log.debug("getRSSI failed: %r", err)
            return None
        if raw > 127:
            raw -= 256
        return raw / 2.0 - RSSI_OFFSET_DB

    def read_lqi(self) -> int | None:
        """Link-quality indicator (lower is better), or None if unavailable."""
        dev = self.dev
        if dev is None:
            return None
        try:
            raw = dev.getLQI()
            return int(raw[0] if isinstance(raw, (bytes, bytearray)) else raw) & 0x7F
        except Exception as err:
            log.debug("getLQI failed: %r", err)
            return None

    # ---- health ----------------------------------------------------------

    @property
    def usb_errors(self) -> int:
        """rflib's count of USB errors that were not plain timeouts."""
        return int(getattr(self.dev, "_usberrorcnt", 0))

    def _wedged(self) -> bool:
        """True when repeated timeouts are accompanied by real USB trouble."""
        if self.dev is None:
            return True
        if getattr(self.dev, "reset_event", None) is not None and self.dev.reset_event.is_set():
            return True
        if getattr(self.dev, "resetup_error", None) is not None:
            return True
        return self._timeouts >= self.max_usb_errors and self.usb_errors > 0

    def heal(self) -> bool:
        """Close, USB-reset and reopen the dongle, restoring the last mode."""
        self._heals += 1
        log.warning("dongle not responding, resetting…")
        self.close()
        usb_reset()
        time.sleep(RESET_SETTLE)
        self.dev = self._open()
        self._common()
        self._timeouts = 0
        if self._rx_kwargs is not None:
            self.configure_rx(**self._rx_kwargs)
        elif self._tx:
            self.configure_tx()
        log.warning("dongle back up")
        return True

    # ---- I/O -------------------------------------------------------------

    def receive(self, timeout_ms: int = 2000) -> tuple[float, bytes] | None:
        """Return ``(timestamp, raw bytes)`` for the next block, or None on timeout."""
        try:
            data, ts = self.dev.RFrecv(timeout=timeout_ms)
        except self.rflib.ChipconUsbTimeoutException:
            self._timeouts += 1
            if self.auto_reset and self._wedged():
                self.heal()
            return None
        except Exception as err:
            self._timeouts += 1
            log.debug("RFrecv failed: %r", err)
            if self.auto_reset and self._wedged():
                self.heal()
                return None
            raise
        self._timeouts = 0
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
        if not self._tx:
            self.configure_tx()
        d = self.dev
        d.makePktFLEN(len(data))
        for n in range(repeat):
            if n:
                time.sleep(gap_s)
            d.RFxmit(data)
        d.setModeIDLE()


#: Backwards-compatible name from before the backends were split out.
Radio = RfcatRadio
