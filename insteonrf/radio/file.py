"""A file-backed backend: replays recorded bit strings, records transmissions.

Used by the tests and by ``--backend file``, so the whole pipeline can be
exercised without a radio.
"""

from __future__ import annotations

import datetime as _dt
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from ..packet import iter_bit_lines


def meta_time(line: str) -> float | None:
    """The time in a ``# <time> ...`` metadata line, or ``None``.

    ``<time>`` is what ``recv -t`` and ``demod -t`` write (ISO 8601, local
    time unless it carries an offset) or epoch seconds. A replay that honours
    it decodes to the same timestamps every time, which is what lets
    time-windowed folding give the same answer at any replay speed.
    """
    parts = line.lstrip("#").split()
    if not parts:
        return None
    tok = parts[0]
    try:
        epoch = float(tok)
    except ValueError:
        pass
    else:
        # A bare small number ("# 3 bursts") is not a time.
        return epoch if 1e9 < epoch < 1e11 else None
    try:
        return _dt.datetime.fromisoformat(tok).timestamp()
    except ValueError:
        return None


class FileRadio:
    """Replay bit-string files (``tests/data/*.txt``) as if they were received."""

    name = "file"
    #: A file will wait; a radio will not. Consumers that must never stall a
    #: live receiver (the dongle's firmware drops a packet without re-arming
    #: DMA when its USB buffer is not drained) may drop work under load, and
    #: this says replay is the one source where that would be a bug instead:
    #: the same file must always decode to the same packets.
    lossless = True

    def __init__(self, paths: Sequence[str | Path] | str | Path = (), *,
                 loop: bool = False, delay_s: float = 0.0, lines: Sequence[str] | None = None):
        if isinstance(paths, (str, Path)):
            paths = [paths]
        self.paths = [Path(p) for p in paths]
        self.loop = loop
        self.delay_s = delay_s
        self._lines = list(lines) if lines is not None else None
        #: Every bit string handed to :meth:`transmit_bits`.
        self.transmitted: list[str] = []
        self._it: Iterator[tuple[float, str]] | None = None
        self.closed = False
        #: True once every line has been replayed (a real radio never exhausts).
        self.exhausted = False

    def __enter__(self) -> FileRadio:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def configure_rx(self, **_: object) -> None:
        """No radio to configure."""

    def configure_tx(self) -> None:
        """No radio to configure."""

    def _read_lines(self) -> list[str]:
        return [line for _, line in self._read_entries()]

    def _read_entries(self) -> list[tuple[float | None, str]]:
        """Bit lines, each with the time its ``# <time> ...`` line gave, if any."""
        if self._lines is not None:
            return [(None, line) for line in self._lines]
        out: list[tuple[float | None, str]] = []
        for path in self.paths:
            when: float | None = None
            for kind, line in iter_bit_lines(path.read_text().splitlines()):
                if kind == "meta":
                    when = meta_time(line)
                elif kind == "bits":
                    out.append((when, line))
                    when = None
        return out

    def iter_bits(self, timeout_ms: int = 0) -> Iterator[tuple[float, str]]:
        """Yield ``(timestamp, bits)``; the timestamp is the recorded one when
        the file has ``# <time>`` lines (``recv -t``, ``demod -t``), else now."""
        while True:
            for when, line in self._read_entries():
                if self.delay_s:
                    time.sleep(self.delay_s)
                yield (time.time() if when is None else when), line
            if not self.loop:
                return

    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None:
        if self._it is None:
            self._it = self.iter_bits()
        got = next(self._it, None)
        if got is None:
            self.exhausted = True
        return got

    def transmit_bits(self, bits: str, *, repeat: int = 1, gap_s: float = 0.0,
                      invert: bool = False) -> None:
        from ..manchester import invert_bits

        if invert:
            bits = invert_bits(bits)
        self.transmitted += [bits] * repeat
