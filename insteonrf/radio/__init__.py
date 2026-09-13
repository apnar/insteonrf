"""Radio backends.

All backends implement :class:`RadioBackend`, so the CLI (and any other
caller) can move bit strings without caring whether they come from a CC1111
dongle, an SDR, or a file:

===========  ==========================================================
``rfcat``    :class:`~insteonrf.radio.rfcat.RfcatRadio` — TX and RX
``rtlsdr``   :class:`~insteonrf.radio.sdr.SdrReceiver` — RX only
``hackrf``   :class:`~insteonrf.radio.sdr.SdrReceiver` /
             :class:`~insteonrf.radio.sdr.SdrTransmitter`
``file``     :class:`~insteonrf.radio.file.FileRadio` — for tests
===========  ==========================================================
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from .file import FileRadio
from .rfcat import (
    CHANNEL_BW,
    DEFAULT_DRATE,
    DEFAULT_FREQ,
    DEVIATION,
    SYNC_PREAMBLE,
    SYNC_START_HEADER,
    USB_PID,
    USB_VID,
    DongleError,
    Radio,
    RfcatRadio,
    usb_reset,
)
from .sdr import DEFAULT_SAMPLE_RATE, SdrReceiver, SdrTransmitter, find_demod

BACKENDS = ("rfcat", "rtlsdr", "hackrf", "file")

#: Every concrete backend. ``open_backend`` returns one of these; they all
#: satisfy :class:`RadioBackend` at runtime.
AnyRadio = RfcatRadio | SdrReceiver | SdrTransmitter | FileRadio


@runtime_checkable
class RadioBackend(Protocol):
    """What the CLI needs from a radio."""

    name: str
    #: True when the source has no more input (files and finished captures);
    #: on a live radio it stays False and ``receive_bits() is None`` means
    #: "nothing arrived within the timeout".
    exhausted: bool

    def configure_rx(self, **kwargs: Any) -> None: ...
    def configure_tx(self) -> None: ...
    def receive_bits(self, timeout_ms: int = 2000) -> tuple[float, str] | None: ...
    def iter_bits(self, timeout_ms: int = 2000) -> Iterator[tuple[float, str]]: ...
    def transmit_bits(self, bits: str, *, repeat: int = ..., gap_s: float = ...,
                      invert: bool = ...) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> RadioBackend: ...
    def __exit__(self, *exc: Any) -> None: ...


def open_backend(name: str = "rfcat", *, freq: int = DEFAULT_FREQ, baud: float = DEFAULT_DRATE,
                 transmit: bool = False, **kwargs: Any) -> AnyRadio:
    """Open one of :data:`BACKENDS` by name.

    Unknown keyword arguments are passed to the backend, so callers can set
    e.g. ``index=``/``auto_reset=`` (rfcat), ``sample_rate=``/``demod=`` (SDR)
    or ``paths=`` (file).
    """
    if name == "rfcat":
        return RfcatRadio(freq, int(baud), **kwargs)
    if name in ("rtlsdr", "hackrf"):
        if transmit:
            if name != "hackrf":
                raise SystemExit("only the hackrf backend can transmit")
            return SdrTransmitter(freq=freq, baud=baud, **kwargs)
        tool = "rtl_sdr" if name == "rtlsdr" else "hackrf_transfer"
        return SdrReceiver(tool, freq=freq, baud=baud, **kwargs)
    if name == "file":
        return FileRadio(**kwargs)
    raise SystemExit(f"unknown backend {name!r}; choose from {', '.join(BACKENDS)}")


__all__ = [
    "BACKENDS",
    "AnyRadio",
    "CHANNEL_BW",
    "DEFAULT_DRATE",
    "DEFAULT_FREQ",
    "DEFAULT_SAMPLE_RATE",
    "DEVIATION",
    "DongleError",
    "FileRadio",
    "Radio",
    "RadioBackend",
    "RfcatRadio",
    "SYNC_PREAMBLE",
    "SYNC_START_HEADER",
    "SdrReceiver",
    "SdrTransmitter",
    "USB_PID",
    "USB_VID",
    "find_demod",
    "open_backend",
    "usb_reset",
]
