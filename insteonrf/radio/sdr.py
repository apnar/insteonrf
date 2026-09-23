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
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np

from .. import dsp
from .rfcat import DEFAULT_DRATE, DEFAULT_FREQ

log = logging.getLogger(__name__)


def _hard_soft(bits: str) -> np.ndarray:
    """±1 confidences for a hard bit string (all a hardware demodulator gives)."""
    a = np.frombuffer(bits.encode("ascii"), dtype=np.uint8)
    return np.where(a == ord("1"), 1.0, -1.0).astype(np.float32)

#: I/Q sample rate the demodulator and the captures in ``Dat/`` use.
DEFAULT_SAMPLE_RATE = dsp.DEFAULT_SAMPLE_RATE
#: How much I/Q to read from the capture tool at a time.
READ_SIZE = 1 << 16
#: Backlog the reader thread may hold before it starts dropping chunks
#: (seconds of I/Q). Demodulating one packet takes ~85 ms of CPU; a Linux
#: pipe holds ~14 ms of samples at 2.4 Msps, so without a reader that keeps
#: draining the pipe, ``rtl_sdr`` silently loses samples during every burst.
QUEUE_SECONDS = 4.0
#: New I/Q demodulated per pass. Bounds both latency and the work per pass.
HOP_S = 0.2
#: I/Q carried over from one pass into the next: at least the longest packet
#: (an extended one is ~100 ms from its sync), so a packet whose sync lies in
#: one pass is always wholly inside it. See :meth:`SdrReceiver._iter_numpy`.
OVERLAP_S = 0.12
#: How many recent reads to remember when estimating when a sample was on
#: the air (see :class:`SampleClock`).
CLOCK_ANCHORS = 256


class SampleClock:
    """Wall-clock time of any sample in the stream, from when reads returned.

    The SDR produces samples at an exact rate, so the time of sample ``s`` is
    ``offset + s / rate`` for one constant ``offset``. Each read gives a
    bound on it: the read's last sample cannot have been on the air *after*
    the read returned. Scheduling, pipe buffering and ``rtl_sdr``'s own block
    size only ever make a read late, never early, so the tightest bound --
    the smallest ``t_read - s_end / rate`` over recent reads -- is the best
    estimate, and it is good to a few milliseconds.

    What it replaces stamped a burst with the time the *buffer* holding it
    was handed to the demodulator, after the squelch had closed and after any
    backlog. Measured against the other receivers that was 0.05 to 2.1 s
    late and never the same twice, and fusion, which groups sightings by
    time, split the same message into separate events.
    """

    def __init__(self, rate: float, anchors: int = CLOCK_ANCHORS):
        self.rate = rate
        self._anchors: list[float] = []
        self._max = anchors

    def note(self, s_end: int, t_read: float) -> None:
        self._anchors.append(t_read - s_end / self.rate)
        if len(self._anchors) > self._max:
            del self._anchors[0]

    def at(self, sample: int) -> float | None:
        if not self._anchors:
            return None
        return min(self._anchors) + sample / self.rate


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
                 squelch: float = 12.0, stdin: bool = False, method: str = "ml"):
        self.freq = freq
        self.sample_rate = sample_rate
        self.baud = baud
        self.squelch = squelch
        #: numpy detector to use: matched-filter ``"ml"`` or ``"discriminator"``.
        self.method = method
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
        # Only the explicit choice gets the C demodulator. It is kept for the
        # regression fixture and for comparison; on live signals it fails where
        # the numpy path succeeds, so "auto" must never silently select it.
        self._use_c = demod == "c"
        self._proc: subprocess.Popen[bytes] | None = None
        self._demod_proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        #: I/Q chunks discarded because demodulation fell behind (numpy path).
        self.dropped_chunks = 0
        #: True once the capture stream has ended (a live SDR never exhausts).
        self.exhausted = False
        #: When each sample was on the air; set up by the reader thread.
        self.clock = SampleClock(sample_rate)

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
        for burst in self.iter_bursts(timeout_ms):
            yield burst.timestamp or time.time(), burst.bits

    def iter_bursts(self, timeout_ms: int = 0) -> Iterator[dsp.Burst]:
        """Yield :class:`~insteonrf.dsp.Burst` objects, soft decisions included.

        The numpy detector produces real confidences; the C binary only hands
        back hard bits, so those bursts carry ±1 and still benefit from the
        Manchester legality check in :mod:`insteonrf.recover`.
        """
        self._start()
        if self._use_c:
            for ts, bits in self._iter_c():
                yield dsp.Burst(bits=bits, soft=_hard_soft(bits), sps=self.sample_rate / self.baud,
                                timestamp=ts)
        else:
            yield from self._iter_numpy()

    def receive_burst(self, timeout_ms: int = 2000) -> dsp.Burst | None:
        """One burst at a time, for callers driving their own loop."""
        # One generator for the life of the receiver: a fresh one per call
        # would re-run _start() and spawn a second capture process, which
        # then fails to claim the device and ends the stream after one burst.
        it = getattr(self, "_bursts", None)
        if it is None:
            it = self._bursts = self.iter_bursts()
        got = next(it, None)
        if got is None:
            self.exhausted = True
        return got

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

    def _iter_numpy(self) -> Iterator[dsp.Burst]:
        """Demodulate in numpy as the samples stream in.

        Passes of ``HOP_S`` new I/Q plus ``OVERLAP_S`` carried over from the
        pass before. A burst is kept by the pass in whose *first* ``HOP_S``
        its sync lies, so every sync is judged exactly once, and always with
        a whole packet after it in the buffer (the overlap is longer than any
        packet). Nothing waits for the squelch to close.

        That waiting is what the previous version did, and it is why this
        receiver missed what followed another message: Insteon traffic comes
        as back-to-back 50 ms slots (a message, its hop repeats, the ACK and
        its repeats, then the next responder), so the squelch stayed open for
        whole exchanges; the buffer grew to its cap, was demodulated in one
        go -- slower than real time, then -- and the backlog grew until the
        reader was throwing I/Q away.
        """
        chunks = self._start_reader()
        rate = self.sample_rate
        hop = 2 * int(HOP_S * rate)
        overlap = 2 * int(OVERLAP_S * rate)
        buf = b""
        base = 0  # sample index of buf[0]
        while True:
            item = chunks.get()
            if item is None:
                yield from self._pass(buf, base, final=True)
                return
            chunk, first = item
            if buf and first != base + len(buf) // 2:
                # The reader dropped I/Q between these: what is buffered is
                # all there will ever be of that stretch, so finish it, and
                # start again at the new position.
                yield from self._pass(buf, base, final=True)
                buf = b""
            if not buf:
                base = first
            buf += chunk
            if len(buf) >= hop + overlap:
                yield from self._pass(buf, base, final=False, keep=overlap)
                cut = len(buf) - overlap
                cut -= cut % 2
                buf = buf[cut:]
                base += cut // 2

    def _pass(self, buf: bytes, base: int, *, final: bool,
              keep: int = 0) -> Iterator[dsp.Burst]:
        """Demodulate one pass; keep the bursts whose sync is this pass's to judge."""
        if len(buf) < 2:
            return
        limit = (len(buf) if final else len(buf) - keep) // 2
        n = len(buf) // 2
        for b in dsp.demodulate_bursts(buf, sample_rate=self.sample_rate, baud=self.baud,
                                       signed=self.signed, squelch=self.squelch,
                                       method=self.method, t0=self.clock.at(base)):
            if b.start >= limit:
                continue  # the next pass sees this one with its whole packet
            if b.header_index is None and not final and b.start + len(b.bits) * b.sps >= n:
                # No sync, and the run runs off the end: it is still arriving.
                # The next pass cannot claim it either (its start is behind
                # that pass), which is the right answer for a headerless
                # fragment anyway.
                continue
            if b.timestamp is None:
                b.timestamp = time.time()
            yield b

    def _start_reader(self) -> queue.Queue[tuple[bytes, int] | None]:
        """Drain the capture pipe on a thread so demodulation never stalls it.

        The main loop demodulates between reads; while it does, the pipe
        fills and the capture tool drops samples (``rtl_sdr`` says so on
        stderr, which is discarded). The thread keeps reading regardless and
        queues chunks; if the demodulator falls behind by more than
        ``QUEUE_SECONDS`` the *newest* chunks are dropped here instead, where
        it is counted in ``dropped_chunks``.

        Each chunk goes with the stream index of its first sample, counted
        over everything read -- dropped chunks included -- so the consumer
        can see a gap, and so :attr:`clock` can tell when any sample was on
        the air.
        """
        chunks: queue.Queue[tuple[bytes, int] | None] = queue.Queue(
            maxsize=max(1, int(QUEUE_SECONDS * 2 * self.sample_rate / READ_SIZE)))
        self.dropped_chunks = 0
        self.clock = SampleClock(self.sample_rate)
        src = self._iq

        def pump() -> None:
            pos = 0  # samples read so far
            carry = b""  # an odd trailing byte: half an I/Q pair
            try:
                while True:
                    chunk = src.read(READ_SIZE)
                    if not chunk:
                        break
                    now = time.time()
                    chunk = carry + chunk
                    if len(chunk) % 2:
                        chunk, carry = chunk[:-1], chunk[-1:]
                    else:
                        carry = b""
                    first = pos
                    pos += len(chunk) // 2
                    self.clock.note(pos, now)
                    try:
                        chunks.put_nowait((chunk, first))
                    except queue.Full:
                        self.dropped_chunks += 1
                        if self.dropped_chunks in (1, 10, 100, 1000):
                            log.warning("demodulator behind by >%.0fs, dropping I/Q (%d chunks so far)",
                                        QUEUE_SECONDS, self.dropped_chunks)
            except (OSError, ValueError):
                pass  # the pipe closed under us (close() ran)
            finally:
                chunks.put(None)

        self._reader = threading.Thread(target=pump, name="sdr-reader", daemon=True)
        self._reader.start()
        return chunks

    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
        burst = self.receive_burst(timeout_ms)
        if burst is None:
            return None
        return burst.timestamp or time.time(), burst.bits


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
