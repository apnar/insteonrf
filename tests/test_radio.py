"""Backend tests. The rfcat paths are exercised against a fake dongle, so the
watchdog logic is covered without USB."""

import pathlib

import pytest

from insteonrf.packet import Packet, parse_bits
from insteonrf.radio import BACKENDS, FileRadio, RadioBackend, open_backend
from insteonrf.radio import rfcat as rfcat_mod

DATA = pathlib.Path(__file__).resolve().parent / "data"


# ---- file backend ----


def test_file_backend_replays_capture():
    with open_backend("file", paths=[DATA / "rfcat-get-engine.txt"]) as radio:
        assert isinstance(radio, RadioBackend)
        lines = list(radio.iter_bits())
    pkts = [p for _, bits in lines for p in parse_bits(bits) if p.crc_ok]
    assert len(lines) == 6 and len(pkts) >= 10


def test_file_backend_records_transmissions():
    pkt = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F)
    radio = FileRadio(lines=[])
    radio.transmit_bits(pkt.to_bits(), repeat=3)
    assert len(radio.transmitted) == 3
    assert parse_bits(radio.transmitted[0])[0].data == pkt.data
    radio.close()
    assert radio.closed


def test_file_backend_receive_bits_one_at_a_time():
    radio = FileRadio(lines=["0101", "1010"])
    assert radio.receive_bits()[1] == "0101"
    assert radio.receive_bits()[1] == "1010"
    assert radio.receive_bits() is None


def test_open_backend_rejects_unknown():
    with pytest.raises(SystemExit):
        open_backend("nope")
    assert "rfcat" in BACKENDS and "file" in BACKENDS


def test_open_backend_transmit_only_hackrf():
    with pytest.raises(SystemExit):
        open_backend("rtlsdr", transmit=True)


# ---- sdr backends ----


def test_sdr_receiver_builds_capture_command():
    from insteonrf.radio.sdr import SdrReceiver

    rx = SdrReceiver("rtl_sdr", freq=914_950_000, sample_rate=2_400_000, gain="19.9",
                     demod="numpy")
    argv = rx._capture_argv()
    assert argv[0] == "rtl_sdr" and "-f" in argv and "914950000" in argv and argv[-1] == "-"
    assert rx.signed is False  # rtl_sdr is unsigned
    hrf = SdrReceiver("hackrf_transfer", demod="numpy")
    assert hrf.signed is True and "-r" in hrf._capture_argv()


def test_sdr_transmitter_modulates_and_dry_runs(caplog):
    from insteonrf.radio.sdr import SdrTransmitter

    pkt = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F)
    tx = SdrTransmitter(dry_run=True)
    samples = tx.modulate(pkt.to_bits())
    assert len(samples) > 1000 and len(samples) % 2 == 0
    tx.transmit_bits(pkt.to_bits())  # must not touch any hardware
    assert "-x" in tx._argv("f.iq") and tx._argv("f.iq")[-1] == "f.iq"
    with pytest.raises(NotImplementedError):
        tx.receive_bits()


def test_sdr_receiver_cannot_transmit():
    from insteonrf.radio.sdr import SdrReceiver

    with pytest.raises(NotImplementedError):
        SdrReceiver("rtl_sdr", demod="numpy").transmit_bits("0101")


# ---- rfcat watchdog, against a fake dongle ----


class FakeTimeout(Exception):
    """Stands in for rflib.ChipconUsbTimeoutException."""


class FakeHandle:
    """Stands in for the pyusb legacy DeviceHandle rflib keeps in ``_do``."""

    def __init__(self):
        self.released = False
        self.finalized = False

    def releaseInterface(self):
        self.released = True

    def finalize(self):
        self.finalized = True


class FakeDongle:
    """Enough of rflib's USBDongle to drive RfcatRadio."""

    def __init__(self, blocks=(), fail_after=None):
        import threading

        self.blocks = list(blocks)
        self._threadGo = threading.Event()
        self._threadGo.set()
        self._do = FakeHandle()
        self.released = False
        self.reset_event = threading.Event()
        self.fail_after = fail_after
        self.calls = 0
        self._usberrorcnt = 0
        self.resetup_error = None
        self.mode = None
        self.sent = []
        self.cleaned = False

    # configuration calls RfcatRadio makes; all no-ops here
    def __getattr__(self, name):
        if name.startswith(("set", "make", "print")):
            return lambda *a, **k: None
        raise AttributeError(name)

    def setModeIDLE(self):
        self.mode = "idle"

    def cleanup(self):
        self.cleaned = True

    def getBuildInfo(self):
        return "FakeDongle r1"

    def RFrecv(self, timeout=0):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            self._usberrorcnt += 1
            raise FakeTimeout("wedged")
        if not self.blocks:
            raise FakeTimeout("no data")
        return bytearray(self.blocks.pop(0)), 1.0

    def RFxmit(self, data):
        self.sent.append(bytes(data))


@pytest.fixture
def fake_radio(monkeypatch):
    """An RfcatRadio wired to FakeDongle, counting how often it reopens."""
    fakes = []

    class FakeRflib:
        MOD_2FSK = 0
        BSCFG_BS_LIMIT_6 = BSCFG_BS_LIMIT_12 = 0
        MFMCFG1_NUM_PREAMBLE_4 = 0
        SYNCM_CARRIER_16_of_16 = SYNCM_CARRIER = SYNCM_16_of_16 = 0
        ChipconUsbTimeoutException = FakeTimeout

    monkeypatch.setattr(rfcat_mod, "_rflib", lambda: FakeRflib)
    monkeypatch.setattr(rfcat_mod, "usb_reset", lambda *a, **k: True)
    # heal() resets from a child process first; never let a test reset a
    # real dongle another process is using (it did, 2026-09-19, and every
    # `make check` knocked the pod's receiver over).
    monkeypatch.setattr(rfcat_mod, "external_usb_reset", lambda *a, **k: True)
    monkeypatch.setattr(rfcat_mod, "RESET_SETTLE", 0)
    monkeypatch.setattr(rfcat_mod, "THREAD_SETTLE", 0)
    monkeypatch.setattr(rfcat_mod.RfcatRadio, "_open", lambda self: _next_fake(fakes))
    return fakes


def _next_fake(fakes):
    dev = FakeDongle(fail_after=0)
    fakes.append(dev)
    return dev


def test_close_stops_rflib_from_reopening(fake_radio):
    """The ctrl thread calls resetup() on reset_event; a closed dongle must not
    re-claim the interface."""
    radio = rfcat_mod.RfcatRadio()
    dev = radio.dev
    radio.close()
    assert dev._closed is True and not dev.reset_event.is_set()


def test_close_releases_the_usb_interface(fake_radio):
    """rflib's cleanup() leaves the interface claimed; close() must hand it back."""
    radio = rfcat_mod.RfcatRadio()
    dev = radio.dev
    radio.configure_rx()
    radio.close()
    assert dev._do is None and dev.cleaned
    assert not dev._threadGo.is_set()
    assert radio.dev is None
    radio.close()  # idempotent


def test_receive_bits_prepends_sync_header(monkeypatch, fake_radio):
    radio = rfcat_mod.RfcatRadio()
    radio.dev.blocks = [b"\xff\x00"]
    radio.configure_rx(sync_header=True)
    radio.dev.fail_after = None
    ts, bits = radio.receive_bits()
    from insteonrf.packet import START_HEADER_INV

    assert bits == START_HEADER_INV + "1111111100000000"
    radio.close()
    assert radio.dev is None


def test_watchdog_heals_a_wedged_dongle(fake_radio):
    radio = rfcat_mod.RfcatRadio(max_usb_errors=3)
    radio.configure_rx()
    assert len(fake_radio) == 1
    for _ in range(3):
        assert radio.receive_bits() is None
    # Three timeouts with USB errors behind them: the dongle was reopened.
    assert len(fake_radio) == 2 and radio._heals == 1
    assert fake_radio[0].cleaned


def test_watchdog_can_be_disabled(fake_radio):
    radio = rfcat_mod.RfcatRadio(auto_reset=False, max_usb_errors=1)
    radio.configure_rx()
    for _ in range(5):
        radio.receive_bits()
    assert len(fake_radio) == 1 and radio._heals == 0


def test_plain_timeouts_do_not_trigger_a_reset(fake_radio):
    radio = rfcat_mod.RfcatRadio(max_usb_errors=2)
    radio.configure_rx()
    radio.dev._usberrorcnt = 0

    def clean_timeout(timeout=0):
        raise FakeTimeout("no traffic")

    radio.dev.RFrecv = clean_timeout
    for _ in range(10):
        radio.receive_bits()
    assert radio._heals == 0 and len(fake_radio) == 1


def test_transmit_bits_packs_and_idles(fake_radio):
    radio = rfcat_mod.RfcatRadio()
    pkt = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F)
    radio.transmit_bits(pkt.to_bits(), repeat=2, gap_s=0)
    assert len(radio.dev.sent) == 2
    bits = "".join(f"{b:08b}" for b in radio.dev.sent[0])
    assert parse_bits(bits)[0].data == pkt.data
    assert radio.dev.mode == "idle"
    with pytest.raises(ValueError):
        radio.transmit_bits("1" * (256 * 8))


def test_usb_reset_missing_device_returns_false(monkeypatch):
    class FakeUsbCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**kw):
            return None

    import sys
    import types

    mod = types.ModuleType("usb")
    mod.core = FakeUsbCore
    monkeypatch.setitem(sys.modules, "usb", mod)
    monkeypatch.setitem(sys.modules, "usb.core", FakeUsbCore)
    assert rfcat_mod.usb_reset(0x1234, 0x5678) is False


def test_usb_reset_gives_up_on_a_hung_reset(monkeypatch):
    import sys
    import threading
    import types

    class HungDevice:
        def reset(self):
            threading.Event().wait(30)

    class FakeUsbCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**kw):
            return HungDevice()

    mod = types.ModuleType("usb")
    mod.core = FakeUsbCore
    monkeypatch.setitem(sys.modules, "usb", mod)
    monkeypatch.setitem(sys.modules, "usb.core", FakeUsbCore)
    assert rfcat_mod.usb_reset(timeout=0.2) is False


def test_open_gives_up_with_a_useful_error(monkeypatch):
    monkeypatch.setattr(rfcat_mod, "_bounded_rfcat_class",
                        lambda: (lambda **kw: (_ for _ in ()).throw(RuntimeError("no dongle"))))
    monkeypatch.setattr(rfcat_mod, "usb_reset", lambda *a, **k: True)
    # heal() resets from a child process first; never let a test reset a
    # real dongle another process is using (it did, 2026-09-19, and every
    # `make check` knocked the pod's receiver over).
    monkeypatch.setattr(rfcat_mod, "external_usb_reset", lambda *a, **k: True)
    monkeypatch.setattr(rfcat_mod, "RESET_SETTLE", 0)
    monkeypatch.setattr(rfcat_mod, "_rflib", lambda: object())
    with pytest.raises(rfcat_mod.DongleError, match="not responding"):
        rfcat_mod.RfcatRadio()


def test_sdr_receiver_numpy_stream_decodes_split_bursts(tmp_path):
    """A burst straddling two reads must still come out once and decode."""
    import numpy as np

    from insteonrf.dsp import modulate_fsk2
    from insteonrf.radio.sdr import READ_SIZE, SdrReceiver

    pkt = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F)
    samples = modulate_fsk2(pkt.to_bits())
    # A packet is several reads long, so this exercises the accumulate-until-
    # the-squelch-closes path as well as a burst starting mid-read.
    assert samples.size > 2 * READ_SIZE
    lead = np.zeros(READ_SIZE // 2, dtype=np.int8)
    stream = np.concatenate([lead, samples, np.zeros(40_000, dtype=np.int8)])
    path = tmp_path / "stream.iq"
    stream.tofile(path)

    rx = SdrReceiver("rtl_sdr", demod="numpy", signed=True)
    with path.open("rb") as fh:
        rx._iq = fh
        rx._start = lambda: None  # already wired to the file
        decoded = [p.data for _, bits in rx.iter_bits() for p in parse_bits(bits) if p.crc_ok]
    assert decoded.count(pkt.data) >= 1
