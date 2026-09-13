"""SDR backends: receive through ``rtl_sdr``/``hackrf_transfer``, transmit
through ``hackrf_transfer``.

These replace the ``rtl_reciv.sh`` / ``hackrf_*.sh`` shell wrappers: the
receiver spawns the SDR tool, pipes its raw I/Q through a demodulator (the C
``fsk2_demod`` binary when it is built, otherwise :mod:`insteonrf.dsp` in
numpy) and yields bit strings; the transmitter modulates bit strings with
:func:`insteonrf.dsp.modulate_fsk2` and hands the samples to
``hackrf_transfer -t``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np

from .. import dsp
from .rfcat import DEFAULT_DRATE, DEFAULT_FREQ

log = logging.getLogger(__name__)

#: I/Q sample rate the demodulator and the captures in ``Dat/`` use.
DEFAULT_SAMPLE_RATE = dsp.DEFAULT_SAMPLE_RATE
#: How much I/Q to read from the capture tool at a time.
READ_SIZE = 1 << 16
#: Give up waiting for the squelch to close after this much signal (a packet
#: is ~56 ms; anything longer is noise or a mis-set squelch).
MAX_BURST_S = 0.5


def find_demod(explicit: str | None = None) -> Path | None:
    """Locate the compiled ``fsk2_demod``: alongside the repo, or on ``PATH``."""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    here = Path(__file__).resolve().parents[2] / "fsk2_demod"
    if here.exists():
        return here
    found = shutil.which("fsk2_demod")
    return Path(found) if found else None


class SdrReceiver:
    """Receive-only backend: an SDR capture tool piped through a demodulator."""

    name = "sdr"

    def __init__(self, tool: Sequence[str] | str = "rtl_sdr", *, freq: int = DEFAULT_FREQ,
                 sample_rate: int = DEFAULT_SAMPLE_RATE, baud: float = DEFAULT_DRATE,
                 demod: str = "auto", demod_path: str | None = None,
                 signed: bool | None = None, gain: str | None = None,
                 squelch: float = 12.0, stdin: bool = False):
        self.freq = freq
        self.sample_rate = sample_rate
        self.baud = baud
        self.squelch = squelch
        self.tool = [tool] if isinstance(tool, str) else list(tool)
        self.kind = Path(self.tool[0]).name if self.tool else "stdin"
        # rtl_sdr writes unsigned 8-bit I/Q, hackrf_transfer signed.
        self.signed = (self.kind.startswith("hackrf") if signed is None else signed)
        self.gain = gain
        self.stdin = stdin
        self.demod = demod
        self.demod_path = find_demod(demod_path)
        if demod == "c" and self.demod_path is None:
            raise RuntimeError("fsk2_demod is not built — run 'make', or use --demod numpy")
        self._use_c = demod == "c" or (demod == "auto" and self.demod_path is not None)
        self._proc: subprocess.Popen[bytes] | None = None
        self._demod_proc: subprocess.Popen[bytes] | None = None
        #: True once the capture stream has ended (a live SDR never exhausts).
        self.exhausted = False

    # ---- lifecycle ----

    def __enter__(self) -> SdrReceiver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for proc in (self._demod_proc, self._proc):
            if proc is None or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proc = self._demod_proc = None

    def configure_rx(self, **_: object) -> None:
        """Nothing to configure: the capture tool is started on first read."""

    def configure_tx(self) -> None:
        raise NotImplementedError("SdrReceiver cannot transmit; use SdrTransmitter")

    def transmit_bits(self, *_: object, **__: object) -> None:
        raise NotImplementedError("SdrReceiver cannot transmit; use SdrTransmitter")

    # ---- capture ----

    def _capture_argv(self) -> list[str]:
        argv = list(self.tool)
        if self.kind == "rtl_sdr":
            argv += ["-f", str(self.freq), "-s", str(self.sample_rate)]
            if self.gain:
                argv += ["-g", self.gain]
            argv += ["-"]
        elif self.kind.startswith("hackrf"):
            argv += ["-f", str(self.freq), "-s", str(self.sample_rate), "-a", "1"]
            if self.gain:
                argv += ["-l", self.gain]
            argv += ["-r", "-"]
        return argv

    def _start(self) -> None:
        if self.stdin:
            self._iq = __import__("sys").stdin.buffer
            return
        argv = self._capture_argv()
        if shutil.which(argv[0]) is None:
            raise RuntimeError(f"{argv[0]} not found on PATH")
        log.info("starting %s", " ".join(argv))
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self._proc.stdout is not None
        self._iq = self._proc.stdout

    def iter_bits(self, timeout_ms: int = 0) -> Iterator[tuple[float, str]]:
        """Yield ``(timestamp, bits)`` per demodulated burst."""
        self._start()
        if self._use_c:
            yield from self._iter_c()
        else:
            yield from self._iter_numpy()

    def _iter_c(self) -> Iterator[tuple[float, str]]:
        argv = [str(self.demod_path), "-s", str(self.sample_rate), "-b", str(int(self.baud)),
                "-U" if self.signed else "-u"]
        log.info("demodulating with %s", " ".join(argv))
        self._demod_proc = subprocess.Popen(argv, stdin=self._iq, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL)
        assert self._demod_proc.stdout is not None
        for raw in self._demod_proc.stdout:
            line = raw.decode("ascii", "replace").strip()
            if line and line[0] in "01":
                yield time.time(), line

    def _iter_numpy(self) -> Iterator[tuple[float, str]]:
        """Demodulate in numpy, one burst at a time.

        A packet is ~56 ms long — about 270 kB of I/Q at 2.4 Msps, several
        reads' worth — so reads are accumulated while the squelch stays open
        and demodulated once the burst ends. ``MAX_BURST_S`` caps the buffer if
        the squelch never closes (a noisy band or too low a threshold).
        """
        tail_bytes = 2 * int(4 * self.sample_rate / self.baud)
        lead_bytes = 2 * int(2 * self.sample_rate / self.baud)
        max_bytes = 2 * int(MAX_BURST_S * self.sample_rate)
        pending = b""
        while True:
            chunk = self._iq.read(READ_SIZE)
            if not chunk:
                yield from self._flush(pending)
                return
            pending += chunk
            busy = self._tail_is_busy(chunk[-tail_bytes:])
            if busy and len(pending) < max_bytes:
                continue  # burst still in progress: keep collecting
            yield from self._flush(pending)
            # Keep a little context so a burst split by the size cap continues.
            pending = pending[-lead_bytes:] if busy else b""

    def _flush(self, buf: bytes) -> Iterator[tuple[float, str]]:
        if not buf:
            return
        ts = time.time()
        for bits in dsp.demodulate_fsk2(buf, sample_rate=self.sample_rate, baud=self.baud,
                                        signed=self.signed, squelch=self.squelch):
            yield ts, bits

    def _tail_is_busy(self, tail: bytes) -> bool:
        """True when the end of a read still carries signal above the squelch."""
        if not tail:
            return False
        a = np.frombuffer(tail, dtype=np.int8 if self.signed else np.uint8).astype(np.float32)
        if not self.signed:
            a -= 128.0
        return bool(np.abs(a[0::2] + 1j * a[1::2]).mean() > self.squelch)

    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
        it = getattr(self, "_it", None)
        if it is None:
            it = self._it = self.iter_bits()
        got = next(it, None)
        if got is None:
            self.exhausted = True
        return got


class SdrTransmitter:
    """Transmit-only backend: numpy modulator + ``hackrf_transfer -t``.

    ``hackrf_transfer`` only reads from a file unless it has been patched to
    accept ``-t -``, so by default the samples go to a temporary file.
    """

    name = "hackrf"
    exhausted = False

    def __init__(self, *, freq: int = DEFAULT_FREQ, sample_rate: int = DEFAULT_SAMPLE_RATE,
                 baud: float = DEFAULT_DRATE, tool: str = "hackrf_transfer",
                 tx_gain: int = 20, amplitude: int = 100, use_stdin: bool = False,
                 dry_run: bool = False):
        self.freq = freq
        self.sample_rate = sample_rate
        self.baud = baud
        self.tool = tool
        self.tx_gain = tx_gain
        self.amplitude = amplitude
        self.use_stdin = use_stdin
        self.dry_run = dry_run

    def __enter__(self) -> SdrTransmitter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Nothing to release: ``hackrf_transfer`` runs once per transmission."""

    def configure_tx(self) -> None:
        """Nothing to configure ahead of time."""

    def configure_rx(self, **_: object) -> None:
        raise NotImplementedError("SdrTransmitter cannot receive; use SdrReceiver")

    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
        raise NotImplementedError("SdrTransmitter cannot receive; use SdrReceiver")

    def iter_bits(self, timeout_ms: int = 0) -> Iterator[tuple[float, str]]:
        raise NotImplementedError("SdrTransmitter cannot receive; use SdrReceiver")

    def modulate(self, bits: str) -> bytes:
        return dsp.modulate_fsk2(bits, sample_rate=self.sample_rate, baud=self.baud,
                                 signed=True, amplitude=self.amplitude).tobytes()

    def _argv(self, target: str) -> list[str]:
        return [self.tool, "-x", str(self.tx_gain), "-a", "1",
                "-s", str(self.sample_rate), "-f", str(self.freq), "-t", target]

    def transmit_bits(self, bits: str, *, repeat: int = 1, gap_s: float = 0.06,
                      invert: bool = False) -> None:
        from ..manchester import invert_bits as _inv

        if invert:
            bits = _inv(bits)
        samples = self.modulate(bits)
        for n in range(repeat):
            if n:
                time.sleep(gap_s)
            self._send(samples)

    def _send(self, samples: bytes) -> None:
        if self.dry_run:
            log.info("dry run: would transmit %d samples", len(samples) // 2)
            return
        if shutil.which(self.tool) is None:
            raise RuntimeError(f"{self.tool} not found on PATH")
        if self.use_stdin:
            # Needs a hackrf_transfer patched to accept '-t -'.
            proc = subprocess.Popen(self._argv("-"), stdin=subprocess.PIPE)
            proc.communicate(samples)
            if proc.returncode:
                raise RuntimeError(f"{self.tool} exited {proc.returncode}")
            return
        fd, path = tempfile.mkstemp(prefix="insteonrf-", suffix=".iq")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(samples)
            subprocess.run(self._argv(path), check=True)
        finally:
            os.unlink(path)
