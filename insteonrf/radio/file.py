"""A file-backed backend: replays recorded bit strings, records transmissions.

Used by the tests and by ``--backend file``, so the whole pipeline can be
exercised without a radio.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from ..packet import iter_bit_lines


class FileRadio:
    """Replay bit-string files (``tests/data/*.txt``) as if they were received."""

    name = "file"

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
        if self._lines is not None:
            return self._lines
        out: list[str] = []
        for path in self.paths:
            for kind, line in iter_bit_lines(path.read_text().splitlines()):
                if kind == "bits":
                    out.append(line)
        return out

    def iter_bits(self, timeout_ms: int = 0) -> Iterator[tuple[float, str]]:
        while True:
            for line in self._read_lines():
                if self.delay_s:
                    time.sleep(self.delay_s)
                yield time.time(), line
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
