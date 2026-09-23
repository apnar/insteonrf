"""Command-line tools. Every stage reads/writes ASCII bit strings (one packet
burst per line; ``#`` lines are metadata) so they can be piped together::

    insteon-rf recv | insteon-rf print
    insteon-rf pkt -s 13.25.80 -d 16.3F.E5 13 00 | insteon-rf send
    insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf modulate -o ping.iq

The SDR stages (``modulate``, ``demod``, ``clip``) speak raw interleaved 8-bit
I/Q on stdin/stdout, the same format ``rtl_sdr`` and ``hackrf_transfer`` use.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from typing import IO, Any, TextIO

from .context import CommandTracker
from .debug import dump_frames
from .packet import Packet, iter_bit_lines, parse_bits
from .radio import (
    BACKENDS,
    DEFAULT_DRATE,
    DEFAULT_FREQ,
    DEFAULT_SAMPLE_RATE,
    DongleError,
    open_backend,
)

log = logging.getLogger("insteonrf")

# --------------------------------------------------------------------------- helpers


def _log_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="more detail; repeat for debug logging")


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity < 1 else (logging.INFO if verbosity == 1 else logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


#: Set by SIGINT/SIGTERM/SIGHUP. Every receive loop checks it between blocks.
STOP = threading.Event()
#: Set by SIGUSR1: the long-running loops log the radio's diagnostics.
DIAG = threading.Event()


def _install_signal_handlers() -> None:
    """Arrange for SIGINT/SIGTERM/SIGHUP to stop the receive loops cleanly.

    Raising ``KeyboardInterrupt`` from the handler is not enough on its own:
    ``rflib``'s ``USBDongle.recv()`` catches ``KeyboardInterrupt``, prints a
    traceback through ``sys.excepthook`` and carries on, so the exception never
    reaches our loop and a ``kill``/Ctrl-C would leave the receiver running —
    and the USB interface claimed. So the handler also sets :data:`STOP`, which
    the loops check after every block, and the exception handler below keeps
    rflib's traceback off the user's terminal.
    """
    STOP.clear()

    def handler(signum: int, _frame: Any) -> None:
        STOP.set()
        raise KeyboardInterrupt(f"signal {signum}")

    def diag_handler(signum: int, _frame: Any) -> None:
        # Ask the running loop to log the radio's own state: a wedged dongle
        # is only understandable from inside the process that holds it
        # (``kill -USR1 1`` in the pod).
        DIAG.set()

    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, diag_handler)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread, or unsupported
                pass

    inner = sys.excepthook

    def quiet(kind: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(kind, KeyboardInterrupt):
            return
        inner(kind, exc, tb)

    sys.excepthook = quiet


def _radio_args(p: argparse.ArgumentParser, *, transmit: bool = False) -> None:
    p.add_argument("--backend", choices=BACKENDS, default="rfcat",
                   help="radio to use (default %(default)s)")
    p.add_argument("-f", "--freq", type=int, default=DEFAULT_FREQ,
                   help="carrier frequency in Hz (default %(default)s)")
    p.add_argument("-B", "--baud", type=int, default=DEFAULT_DRATE,
                   help="data rate in baud (default %(default)s)")
    p.add_argument("--index", type=int, default=0,
                   help="rfcat device index when several dongles are attached")
    p.add_argument("--no-auto-reset", action="store_true",
                   help="do not USB-reset and reopen an unresponsive rfcat dongle")
    p.add_argument("-s", "--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE,
                   help="SDR backends: I/Q sample rate (default %(default)s)")
    p.add_argument("--replay", action="append", metavar="FILE",
                   help="file backend: bit-string file to replay (repeatable)")
    if transmit:
        p.add_argument("--tx-gain", type=int, default=20, help="hackrf TX VGA gain in dB")
    else:
        p.add_argument("--demod", choices=("auto", "c", "numpy"), default="numpy",
                       help="SDR backends: demodulator to use (default %(default)s). 'auto' also "
                            "means numpy; the C fsk2_demod is only used when asked for by name -- "
                            "it fails on real signals the numpy path decodes (see CLAUDE.md), and "
                            "an 'auto' that silently picked it whenever 'make' had been run cost a "
                            "first live RTL-SDR test every packet")
        p.add_argument("-m", "--method", choices=("ml", "discriminator"), default="ml",
                       help="SDR backends, numpy demodulator: matched-filter ML (default, more "
                            "sensitive and gives soft decisions) or phase discriminator")
        p.add_argument("--gain", help="SDR backends: receiver gain setting")


def _open_radio(a: argparse.Namespace, *, transmit: bool = False) -> Any:
    kw: dict[str, Any] = {}
    if a.backend == "rfcat":
        kw.update(index=a.index, auto_reset=not a.no_auto_reset)
    elif a.backend in ("rtlsdr", "hackrf"):
        kw.update(sample_rate=a.sample_rate)
        if transmit:
            kw.update(tx_gain=a.tx_gain)
        else:
            kw.update(demod=a.demod, gain=a.gain)
            if getattr(a, "method", None):
                kw["method"] = a.method
    elif a.backend == "file":
        kw.update(paths=a.replay or [])
    return open_backend(a.backend, freq=a.freq, baud=a.baud, transmit=transmit, **kw)


def _open_lines(paths: list[str] | None) -> Iterable[str]:
    if not paths or paths == ["-"]:
        yield from sys.stdin
        return
    for path in paths:
        with open(path) as fh:
            yield from fh


def _open_iq(paths: list[str] | None) -> Iterator[IO[bytes]]:
    """Yield file objects of raw I/Q input (stdin when no paths are given)."""
    if not paths or paths == ["-"]:
        yield sys.stdin.buffer
        return
    for path in paths:
        with open(path, "rb") as fh:
            yield fh


def _print_packets(packets: list[Packet], out: TextIO, *, verbose: bool = False,
                   show_time: bool = False, log_fh: TextIO | None = None,
                   as_json: bool = False) -> None:
    for p in packets:
        if as_json:
            out.write(json.dumps(p.to_dict(), separators=(",", ":")) + "\n")
        else:
            if show_time:
                out.write(p.time_str + "  ")
            out.write((p.describe() if verbose else p.summary()) + "\n")
        if log_fh is not None:
            log_fh.write(p.hex_line() + "\n")
    out.flush()
    if log_fh is not None:
        log_fh.flush()


def _keep(packets: list[Packet], show_all: bool) -> list[Packet]:
    return packets if show_all else [q for q in packets if q.calc_crc is not None]


def _receive(radio: Any, timeout_ms: int
             ) -> tuple[float, str, Any, int | None, float | None] | None:
    """One burst from any backend, keeping soft decisions where they exist.

    A hardware demodulator (the rfcat dongle) can only give hard bits; the
    numpy SDR path also returns per-symbol confidence, the located frame
    grid and the measured symbol SNR, which :func:`_decode` and the mesh
    capture put to work.
    """
    if hasattr(radio, "receive_burst"):
        burst = radio.receive_burst(timeout_ms)
        if burst is None:
            return None
        return (burst.timestamp or time.time(), burst.bits, burst.soft, burst.header_index,
                burst.snr_db)
    got = radio.receive_bits(timeout_ms)
    if got is None:
        return None
    return got[0], got[1], None, None, None


def _decode(bits: str, ts: float | None, *, repair: bool = True, show_all: bool = False,
            soft: Any = None, header_index: int | None = None,
            tracker: Any = None) -> list[Packet]:
    """Decode a burst, recovering marginal packets when ``repair`` is set.

    :func:`~insteonrf.packet.parse_bits` is the fast path. With ``repair`` on,
    :mod:`insteonrf.recover` also runs: it soft-combines Manchester pairs (or,
    on hard bits from the dongle, treats illegal pairs as the suspect
    positions), checks the known frame-index counters, and flips the least
    confident bits looking for a CRC match. Anything it recovers that
    ``parse_bits`` did not is added to the result.
    """
    found = parse_bits(bits, ts)
    if not repair:
        return _keep(found, show_all)
    from .recover import recover_packets

    def key(pkt: Packet) -> tuple[int, ...]:
        # Everything up to and including the CRC. A truncated second sighting
        # of the same message differs only in how much pad survived, so this
        # keeps one copy of it rather than two.
        return tuple(pkt.data[: pkt.crc_index + 1])

    out: list[Packet] = []
    seen: set[tuple[int, ...]] = set()
    for rec in recover_packets(bits, soft, ts, header_index=header_index):
        out.append(rec.packet)
        seen.add(key(rec.packet))
    for pkt in _keep(found, show_all):
        # A packet whose frame counters are wrong is CRC luck, not a packet.
        if pkt.index_ok is False and not show_all:
            continue
        if key(pkt) not in seen:
            out.append(pkt)
            seen.add(key(pkt))
    if tracker is not None:
        # An ACK echoes its query's cmd1 but is always a standard message, so
        # the reply is only nameable in the light of what was asked.
        for pkt in out:
            tracker.observe(pkt)
    return out



class _Liveness:
    """When a real packet last arrived, shared with the decode worker.

    The receive loop must not decode, so it cannot see for itself whether a
    block held a packet; the worker sets :attr:`last_packet` and the loop
    only reads it. Plain attributes on purpose -- these are single stores of
    an immutable value, the watchdog wants an approximate answer, and a lock
    on the receive thread is exactly the sort of thing this change exists to
    remove.
    """

    __slots__ = ("last_packet", "junk_blocks", "dropped")

    def __init__(self) -> None:
        self.last_packet = time.time()
        #: Blocks since the last packet that held no packet at all. A deaf
        #: dongle still false-syncs on noise, so these are not liveness.
        self.junk_blocks = 0
        #: Blocks the receive loop had to throw away because the worker was
        #: too far behind. Should stay at zero; see ``--queue``.
        self.dropped = 0


def _silence_action(quiet_s: float, max_silence: float) -> str:
    """What to do about ``quiet_s`` seconds with no decoded packet.

    A receiver that has gone deaf is worse than one that has crashed: it
    reports nothing, logs nothing, and every downstream measurement reads as
    "there was no traffic" rather than as a fault. That matters most for the
    miss table, where a deaf radio produces exactly the wrong conclusion --
    "the PLM misses nothing" -- with no indication anything is wrong.

    rflib's own recovery does not cover this case: it triggers on receive
    timeouts *plus* USB errors, so a radio that has fallen out of RX while the
    USB path still answers looks healthy and simply returns nothing.

    There is exactly one step, and it is cheap: re-arm receive. Measured on
    2026-09-21 against the Heltec as a reference clock, the dongle wedges
    *mid-run*, within minutes, while still answering register reads and still
    reporting ``MARC_STATE_RX`` with a correct modem configuration and a
    normal noise floor -- and then delivers not one block, not even the noise
    false-syncs, until it is re-armed. Re-arming is what recovers it: the pod
    caught 9 of 9 probes, went deaf, missed 8, and came back on the tick
    exactly five minutes after its last decode, which is when the old
    threshold re-armed it.

    A **USB reset is not the remedy** and used to be the second step here.
    Over one day only 8 of 128 resets were followed by a decode inside a
    minute and the median wait for the next one was half an hour, while the
    dongle was re-enumerated 200 times for nothing. It is now reached only
    when the dongle stops answering at all, which the caller decides from
    :meth:`~insteonrf.radio.rfcat.RfcatRadio.diagnostics`, and by
    ``RfcatRadio.receive`` itself on repeated USB errors.

    So the threshold wants to be seconds, not the half hour it was: a re-arm
    costs a handful of register writes and can only lose a packet that
    happens to be mid-flight, against whole spells of deafness if it waits.
    """
    if max_silence <= 0:
        return "none"
    if quiet_s > max_silence:
        return "rearm"
    return "none"


def _ts_line(ts: float, nbits: int) -> str:
    t = _dt.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")
    return f"# {t} len={nbits // 8}"


# --------------------------------------------------------------------------- recv


def recv_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf recv",
                                description="Receive Insteon RF and print raw bit strings.")
    _radio_args(p)
    p.add_argument("-t", "--time", action="store_true", help="emit a '# <timestamp> len=N' line before each "
                                                             "burst")
    p.add_argument("-D", "--decode", action="store_true", help="decode packets here instead of printing raw "
                                                               "bits")
    p.add_argument("-j", "--json", action="store_true", help="with -D: one JSON object per packet")
    p.add_argument("-a", "--all", action="store_true", help="with -D: also show fragments shorter than a "
                                                            "full packet")
    p.add_argument("--carrier", action="store_true",
                   help="capture on carrier detect alone instead of syncing on the Insteon start header "
                        "(noisier, but shows bursts the sync word misses)")
    p.add_argument("--timeout", type=int, default=2000, help="USB receive timeout in ms (default "
                                                             "%(default)s)")
    p.add_argument("--no-repair", action="store_true",
                   help="with -D: do not try to recover packets whose CRC failed")
    _log_args(p)
    a = p.parse_args(argv)
    _setup_logging(a.verbose)
    _install_signal_handlers()

    tracker = CommandTracker()
    with _open_radio(a) as radio:
        radio.configure_rx(sync_header=not a.carrier)
        if a.verbose and not a.decode and hasattr(radio, "print_config"):
            radio.print_config()
        try:
            while not STOP.is_set():
                got = _receive(radio, a.timeout)
                if got is None:
                    if getattr(radio, "exhausted", False):
                        break
                    continue  # nothing on the air within the timeout
                ts, bits, soft, header_index, _snr = got
                if a.decode:
                    pkts = _decode(bits, ts, repair=not a.no_repair, show_all=a.all,
                                   soft=soft, header_index=header_index, tracker=tracker)
                    _print_packets(pkts, sys.stdout, verbose=a.verbose > 0,
                                   show_time=a.time, as_json=a.json)
                else:
                    if a.time:
                        print(_ts_line(ts, len(bits)))
                    print(bits)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            pass
    return 0


# --------------------------------------------------------------------------- send


def send_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf send",
                                description="Transmit bit strings (from 'insteon-rf pkt').")
    _radio_args(p, transmit=True)
    p.add_argument("-r", "--repeat", type=int, default=1, help="send each line this many times (default 1)")
    p.add_argument("--gap-ms", type=int, default=60, help="gap between repeats in ms (default %(default)s)")
    p.add_argument("-i", "--invert", action="store_true", help="invert bits before sending")
    p.add_argument("-L", "--listen", type=int, default=0, metavar="MS",
                   help="after each transmission, receive for MS milliseconds and print decoded replies "
                        "(rfcat backend only)")
    p.add_argument("-n", "--dry-run", action="store_true", help="do everything except key the transmitter")
    _log_args(p)
    p.add_argument("files", nargs="*", help="bit-string files (default: stdin)")
    a = p.parse_args(argv)
    _setup_logging(a.verbose)
    _install_signal_handlers()

    if a.dry_run and a.backend == "rfcat":
        a.backend = "file"
    sent = 0
    with _open_radio(a, transmit=True) as radio:
        if a.dry_run and hasattr(radio, "dry_run"):
            radio.dry_run = True
        for kind, line in iter_bit_lines(_open_lines(a.files)):
            if kind != "bits":
                continue
            for q in parse_bits(line):
                (log.warning if a.verbose else log.info)("tx: %s", q.summary())
            radio.configure_tx()
            radio.transmit_bits(line, repeat=a.repeat, gap_s=a.gap_ms / 1000, invert=a.invert)
            sent += 1
            if a.listen:
                radio.configure_rx(sync_header=True)
                end = time.monotonic() + a.listen / 1000
                while (left := end - time.monotonic()) > 0 and not STOP.is_set():
                    r = radio.receive_bits(max(1, int(left * 1000)))
                    if r is not None:
                        _print_packets(_decode(r[1], r[0]), sys.stdout, show_time=True)
    log.info("sent %d packet(s)", sent)
    return 0


# --------------------------------------------------------------------------- print


def print_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf print",
                                description="Decode Insteon packets from bit strings on stdin (or files).")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="multi-line decode with addresses and command names")
    p.add_argument("-j", "--json", action="store_true", help="one JSON object per packet")
    p.add_argument("-t", "--time", action="store_true", help="prefix each packet with the time it was "
                                                             "decoded")
    p.add_argument("-a", "--all", action="store_true", help="also show fragments too short to have a CRC")
    p.add_argument("-l", "--log", metavar="FILE", help="append the hex line of every packet to FILE")
    p.add_argument("--no-repair", action="store_true",
                   help="do not try to recover packets whose CRC failed")
    p.add_argument("files", nargs="*", help="input files (default: stdin)")
    a = p.parse_args(argv)

    log_fh = open(a.log, "a") if a.log else None
    tracker = CommandTracker()
    try:
        for kind, line in iter_bit_lines(_open_lines(a.files)):
            if kind == "meta":
                if not a.json:
                    print(line)
                continue
            if kind == "junk":
                print(f"skipping non-bit line: {line[:40]!r}", file=sys.stderr)
                continue
            _print_packets(_decode(line, None, repair=not a.no_repair, show_all=a.all,
                                   tracker=tracker),
                           sys.stdout, verbose=a.verbose > 0, show_time=a.time,
                           log_fh=log_fh, as_json=a.json)
    except KeyboardInterrupt:
        pass
    finally:
        if log_fh:
            log_fh.close()
    return 0


# --------------------------------------------------------------------------- pkt


def _hexbyte(s: str) -> int:
    v = int(s, 16)
    if not 0 <= v <= 0xFF:
        raise argparse.ArgumentTypeError(f"not a byte: {s}")
    return v


def build_from_args(a: argparse.Namespace) -> Packet:
    if a.raw:
        words = [w for w in a.words if w != ":"]
        data = [int(w, 16) for w in words]
        if len(data) < 9:
            raise SystemExit("raw packets need at least 9 bytes: flags, to(3), from(3), cmd1, cmd2")
        add_crc = len(data) in (9, 23)
        pkt = Packet.from_wire(data, crc=add_crc, pad=a.pad or add_crc)
        # Flag overrides on raw packets.
        flags = pkt.data[0]
        if a.ack is not None:
            flags = (flags | 0x20) if a.ack else (flags & ~0x20)
        if a.hops_left is not None:
            flags = (flags & ~0x0C) | ((a.hops_left & 3) << 2)
        if a.max_hops is not None:
            flags = (flags & ~0x03) | (a.max_hops & 3)
        if flags != pkt.data[0]:
            pkt.data[0] = flags
            if add_crc and pkt.calc_crc is not None:
                pkt.data[pkt.crc_index] = pkt.calc_crc
        return pkt

    if a.src is None:
        raise SystemExit("-s/--src is required (the address the packet claims to come from)")
    if (a.dst is None) == (a.group is None):
        raise SystemExit("give exactly one of -d/--dst or -g/--group")
    if not a.words:
        raise SystemExit("cmd1 is required (hex), e.g. '11 FF' for On at full level")
    cmd1 = _hexbyte(a.words[0])
    cmd2 = _hexbyte(a.words[1]) if len(a.words) > 1 else 0
    ext_data = [_hexbyte(w) for w in a.words[2:]]
    if ext_data and not a.extended:
        a.extended = True
    return Packet.build(
        a.src, a.dst, group=a.group, cmd1=cmd1, cmd2=cmd2, ext_data=ext_data,
        extended=a.extended, bcast=a.broadcast, ack=bool(a.ack),
        max_hops=3 if a.max_hops is None else a.max_hops,
        hops_left=3 if a.hops_left is None else a.hops_left,
        pad=True,
    )


def pkt_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="insteon-rf pkt",
        description="Build an Insteon packet and print it as a transmit-ready bit string.",
        epilog="examples:\n"
               "  insteon-rf pkt -s 13.25.80 -d 16.3F.E5 13 00        # Off\n"
               "  insteon-rf pkt -s 13.25.80 -d 16.3F.E5 11 BF        # On at 75%%\n"
               "  insteon-rf pkt -s 13.25.80 -g 1 -b 11 00            # group 1 broadcast On\n"
               "  insteon-rf pkt -r 0B E5 3F 16 80 25 13 11 BF        # raw wire bytes, CRC added\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-s", "--src", help="source address (e.g. 13.25.80) — must be linked to the target for "
                                       "i2cs devices")
    p.add_argument("-d", "--dst", help="destination address")
    p.add_argument("-g", "--group", type=lambda s: int(s, 0), help="all-link group number (instead of -d)")
    p.add_argument("-e", "--extended", action="store_true", help="extended (14-byte payload) packet")
    p.add_argument("-b", "--broadcast", action="store_true", help="set the broadcast flag")
    p.add_argument("-a", "--ack", dest="ack", action="store_true", default=None, help="set the ACK flag")
    p.add_argument("--no-ack", dest="ack", action="store_false", help="clear the ACK flag (raw packets)")
    p.add_argument("-m", "--max-hops", type=int, choices=range(4), help="max hops (default 3)")
    p.add_argument("-l", "--hops-left", type=int, choices=range(4), help="hops left (default 3)")
    p.add_argument("-c", "--count", type=int, default=1, help="emit the packet this many times, one per line")
    p.add_argument("-I", "--no-invert", action="store_true", help="emit logical instead of on-air (inverted) "
                                                                  "bits")
    p.add_argument("-p", "--pad", action="store_true", help="append pad bytes to a raw packet")
    p.add_argument("-r", "--raw", action="store_true", help="words are raw wire bytes in hex (':' separators "
                                                            "ignored)")
    p.add_argument("-n", "--dry-run", action="store_true", help="describe the packet on stderr, print "
                                                                "nothing")
    p.add_argument("-j", "--json", action="store_true", help="print the packet as JSON instead of bits")
    p.add_argument("-v", "--verbose", action="count", default=0, help="describe the packet on stderr as well")
    p.add_argument("words", nargs="*", help="cmd1 [cmd2] [ext data...] in hex, or raw bytes with -r")
    a = p.parse_args(argv)

    pkt = build_from_args(a)
    if a.verbose or a.dry_run:
        print(pkt.describe(), file=sys.stderr)
        print(f"{len(pkt.data)} bytes -> {len(pkt.to_bits())} bits on air", file=sys.stderr)
    if a.dry_run:
        return 0
    if a.json:
        print(json.dumps(pkt.to_dict(), separators=(",", ":")))
        return 0
    bits = pkt.to_bits(invert=not a.no_invert)
    for _ in range(a.count):
        print(bits)
    return 0


# --------------------------------------------------------------------------- dump


def dump_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="insteon-rf dump",
        description="Frame-by-frame breakdown of bit strings, for debugging a demodulator.")
    p.add_argument("files", nargs="*", help="input files (default: stdin)")
    a = p.parse_args(argv)
    for kind, line in iter_bit_lines(_open_lines(a.files)):
        if kind == "bits":
            print(dump_frames(line))
            print()
    return 0


# --------------------------------------------------------------------------- modulate / demod / clip


def _iq_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-s", "--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE,
                   help="I/Q sample rate in Hz (default %(default)s)")
    p.add_argument("-b", "--baud", type=float, default=DEFAULT_DRATE,
                   help="data rate in baud (default %(default)s)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-U", "--signed", dest="signed", action="store_true", default=True,
                   help="signed 8-bit I/Q, HackRF style (default)")
    g.add_argument("-u", "--unsigned", dest="signed", action="store_false",
                   help="unsigned 8-bit I/Q offset by 128, rtl-sdr style")


def modulate_main(argv: list[str] | None = None) -> int:
    from . import dsp

    p = argparse.ArgumentParser(
        prog="insteon-rf modulate",
        description="Modulate bit strings into raw 8-bit I/Q for an SDR transmitter.",
        epilog="example:\n"
               "  insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf modulate -o ping.iq\n"
               "  hackrf_transfer -x 20 -a 1 -s 2400000 -f 914950000 -t ping.iq\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _iq_args(p)
    p.add_argument("-d", "--deviation", type=float, default=dsp.DEFAULT_DEVIATION,
                   help="FSK deviation in Hz (default %(default)s)")
    p.add_argument("-A", "--amplitude", type=int, default=dsp.DEFAULT_AMPLITUDE,
                   help="sample amplitude, 1-127 (default %(default)s)")
    p.add_argument("--preamble-ms", type=float, default=2.0, help="preamble tone length (default "
                                                                  "%(default)s)")
    p.add_argument("--trailer-ms", type=float, default=1.0, help="trailing tone length (default %(default)s)")
    p.add_argument("-o", "--out", metavar="FILE", help="write to FILE instead of stdout")
    _log_args(p)
    p.add_argument("files", nargs="*", help="bit-string files (default: stdin)")
    a = p.parse_args(argv)
    _setup_logging(a.verbose)

    out = open(a.out, "wb") if a.out else sys.stdout.buffer
    n = 0
    try:
        for kind, line in iter_bit_lines(_open_lines(a.files)):
            if kind != "bits":
                continue
            samples = dsp.modulate_fsk2(
                line, sample_rate=a.sample_rate, baud=a.baud, deviation=a.deviation,
                signed=a.signed, amplitude=a.amplitude,
                preamble_s=a.preamble_ms / 1000, trailer_s=a.trailer_ms / 1000)
            out.write(samples.tobytes())
            n += 1
            log.info("modulated %d bits -> %d samples", len(line), samples.size // 2)
        out.flush()
    finally:
        if a.out:
            out.close()
    log.info("modulated %d burst(s)", n)
    return 0


def demod_main(argv: list[str] | None = None) -> int:
    from . import dsp
    from .radio.sdr import find_demod

    p = argparse.ArgumentParser(prog="insteon-rf demod",
                                description="Demodulate raw 8-bit I/Q into bit strings.")
    _iq_args(p)
    p.add_argument("--demod", choices=("auto", "c", "numpy"), default="numpy",
                   help="numpy (default) or the compiled fsk2_demod binary")
    p.add_argument("-m", "--method", choices=("ml", "discriminator"), default="ml",
                   help="numpy detector: matched-filter ML (default) or phase discriminator")
    p.add_argument("-D", "--decode", action="store_true",
                   help="decode packets here, keeping soft decisions (better sensitivity "
                        "than piping bits to 'print', which discards them)")
    p.add_argument("-j", "--json", action="store_true", help="with -D: one JSON object per packet")
    p.add_argument("--no-repair", action="store_true",
                   help="with -D: do not attempt CRC-guided bit repair")
    p.add_argument("-t", "--time", action="store_true",
                   help="emit a '# <timestamp> len=N' line per burst (numpy demodulator only)")
    p.add_argument("-q", "--squelch", type=float, default=12.0,
                   help="squelch level, numpy demodulator only (default %(default)s)")
    _log_args(p)
    p.add_argument("files", nargs="*", help="raw I/Q files (default: stdin)")
    a = p.parse_args(argv)
    _setup_logging(a.verbose)

    tracker = CommandTracker()
    if a.demod in ("c", "auto"):
        path = find_demod()
        if path is None and a.demod == "c":
            raise SystemExit("fsk2_demod is not built — run 'make', or use --demod numpy")
        if path is not None:
            import subprocess

            argv2 = [str(path), "-s", str(a.sample_rate), "-b", str(int(a.baud)),
                     "-U" if a.signed else "-u"]
            log.info("running %s", " ".join(argv2))
            rc = 0
            for fh in _open_iq(a.files):
                rc |= subprocess.run(argv2, stdin=fh).returncode
            return rc

    for fh in _open_iq(a.files):
        raw = fh.read()
        bursts = dsp.demodulate_bursts(raw, sample_rate=a.sample_rate, baud=a.baud,
                                       signed=a.signed, squelch=a.squelch, method=a.method,
                                       timestamp=time.time())
        for burst in bursts:
            if a.decode:
                from .recover import recover_from_burst

                for rec in recover_from_burst(burst, repair=not a.no_repair):
                    tracker.observe(rec.packet)
                    if a.verbose:
                        log.info("burst: %d bits, snr %.1f dB, cfo %+.0f Hz, %d bit(s) repaired",
                                 len(burst.bits), burst.snr_db, burst.cfo_hz, rec.corrected)
                    _print_packets([rec.packet], sys.stdout, verbose=a.verbose > 0,
                                   show_time=a.time, as_json=a.json)
                continue
            if a.time:
                print(_ts_line(burst.timestamp or time.time(), len(burst.bits)))
            print(burst.bits)
    sys.stdout.flush()
    return 0


def clip_main(argv: list[str] | None = None) -> int:
    from . import dsp

    p = argparse.ArgumentParser(prog="insteon-rf clip",
                                description="Split a raw I/Q stream into one file per burst (like rf_clip).")
    _iq_args(p)
    p.add_argument("-o", "--prefix", default="burst", help="output file prefix (default %(default)s)")
    p.add_argument("-q", "--squelch", type=float, default=12.0, help="squelch level (default %(default)s)")
    _log_args(p)
    p.add_argument("files", nargs="*", help="raw I/Q files (default: stdin)")
    a = p.parse_args(argv)
    _setup_logging(a.verbose)

    n = 0
    for fh in _open_iq(a.files):
        for burst in dsp.iq_bursts(fh.read(), sample_rate=a.sample_rate, baud=a.baud,
                                   signed=a.signed, squelch=a.squelch):
            name = f"{a.prefix}-{n:04d}.dat"
            with open(name, "wb") as out:
                out.write(burst.tobytes())
            print(f"{name}  {burst.size // 2} samples")
            n += 1
    log.info("wrote %d burst(s)", n)
    return 0


# --------------------------------------------------------------------------- monitor


def monitor_main(argv: list[str] | None = None) -> int:
    from .monitor import DEDUPE_WINDOW_S, Deduper, JsonlWriter, MqttPublisher

    p = argparse.ArgumentParser(
        prog="insteon-rf monitor",
        description="Log every Insteon packet on the air as JSON lines, and optionally to MQTT.")
    _radio_args(p)
    p.add_argument("-o", "--out", metavar="FILE", help="append JSON lines to FILE (rotated)")
    p.add_argument("--max-bytes", type=int, default=32 << 20, help="rotate FILE at this size (default "
                                                                   "%(default)s)")
    p.add_argument("--backups", type=int, default=5, help="how many rotated files to keep (default "
                                                          "%(default)s)")
    p.add_argument("--mqtt", metavar="HOST[:PORT]", help="publish each packet to this MQTT broker")
    p.add_argument("--topic", default="insteon-rf", help="MQTT topic prefix (default %(default)s)")
    p.add_argument("--mqtt-alerts-only", action="store_true",
                   help="publish only all-on triggers to <topic>/alert, not every packet — the "
                        "JSON-lines file is the record, and a per-packet stream nobody subscribes "
                        "to only costs broker log volume")
    p.add_argument("--mqtt-retain-alerts", action="store_true",
                   help="retain the last alert so a consumer connecting later still sees it "
                        "(it will re-fire on every reconnect)")
    p.add_argument("--mqtt-user", default=os.environ.get("INSTEONRF_MQTT_USER"),
                   help="MQTT username (default: $INSTEONRF_MQTT_USER)")
    p.add_argument("--mqtt-pass", default=os.environ.get("INSTEONRF_MQTT_PASS"),
                   help="MQTT password (default: $INSTEONRF_MQTT_PASS — prefer this to a "
                        "command line, which is visible in ps)")
    p.add_argument("--mesh-capture", metavar="NAME",
                   help="also publish every raw burst to <topic>/rx/NAME in the listener-board "
                        "capture format, so this receiver joins the mesh that 'insteon-rf mesh' "
                        "fuses — the dongle is a valid mesh member, and the only one that can "
                        "produce soft decisions")
    p.add_argument("--no-dedupe", action="store_true", help="log every mesh repeat separately")
    p.add_argument("-U", "--unknown-commands", action="store_true",
                   help="report commands the tables do not name — the first time each is seen "
                        "and as a summary at exit — so a device speaking something new surfaces "
                        "instead of hiding in the log as 'Std Command 0x??'")
    p.add_argument("--window", type=float, default=DEDUPE_WINDOW_S,
                   help="dedupe window in seconds (default %(default)s)")
    p.add_argument("-W", "--watch-all-on", action="store_true",
                   help="watch for phantom 'all on' triggers: a group-0 all-link broadcast, an "
                        "On to an unknown group, or a broadcast storm")
    p.add_argument("--dump-dir", metavar="DIR",
                   help="with -W: write the trigger plus the surrounding raw bursts here")
    p.add_argument("--known-groups", metavar="FILE",
                   help="with -W: groups the network legitimately uses, so a novel group number "
                        "counts as suspicious (one per line, or a JSON array)")
    p.add_argument("--context-s", type=float, default=30.0,
                   help="with -W: seconds of raw bursts to keep around a trigger")
    p.add_argument("-a", "--all", action="store_true", help="also log fragments with no CRC")
    p.add_argument("--quiet", action="store_true", help="do not echo records to stdout")
    p.add_argument("--carrier", action="store_true", help="capture on carrier detect alone")
    p.add_argument("--no-rssi", action="store_true",
                   help="do not sample the dongle's RSSI register after each block")
    p.add_argument("--no-repair", action="store_true",
                   help="do not try to recover packets whose CRC failed")
    p.add_argument("--timeout", type=int, default=2000, help="USB receive timeout in ms")
    p.add_argument("--max-silence", type=float, default=20.0, metavar="SECONDS",
                   help="re-arm the receiver after this many seconds with no decoded packet "
                        "(0 disables). A dongle that wedges goes quiet without erroring, which "
                        "reads downstream as 'there was no traffic' rather than as a fault, and "
                        "re-arming is what recovers it -- so this is seconds, not minutes "
                        "(default %(default)s)")
    p.add_argument("--diag-after", type=float, default=300.0, metavar="SECONDS",
                   help="after this long with no decoded packet, log what the radio says it is "
                        "doing, and USB-reset it if it has stopped answering altogether "
                        "(default %(default)s)")
    p.add_argument("--queue", type=int, default=512, metavar="N",
                   help="blocks that may be waiting to be decoded. The receive thread does "
                        "nothing but drain the radio; everything else happens behind this "
                        "queue (default %(default)s)")
    _log_args(p)
    a = p.parse_args(argv)
    _setup_logging(a.verbose or 1)
    _install_signal_handlers()

    writer = JsonlWriter(a.out, max_bytes=a.max_bytes, backups=a.backups) if a.out else None
    mqtt = None
    if a.mqtt:
        host, _, port = a.mqtt.partition(":")
        mqtt = MqttPublisher(host, int(port or 1883), a.topic,
                             username=a.mqtt_user, password=a.mqtt_pass,
                             alerts_only=a.mqtt_alerts_only,
                             alert_retain=a.mqtt_retain_alerts)
    dd = None if a.no_dedupe else Deduper(a.window)
    tracker = CommandTracker()
    watcher = None
    if a.watch_all_on:
        from .allon import AllOnWatcher, load_known_groups

        watcher = AllOnWatcher(
            known_groups=load_known_groups(a.known_groups) if a.known_groups else None,
            window_s=a.context_s, dump_dir=a.dump_dir)
        log.info("watching for phantom all-on triggers%s",
                 f", dumping context to {a.dump_dir}" if a.dump_dir else "")
    seen = 0
    unknown: Counter[tuple[str, int]] = Counter()

    def note_unknown(pkt: Packet) -> None:
        """Flag a command the tables cannot name, once per distinct command."""
        from . import cmds

        if pkt.cmd1 is None or cmds.is_known(pkt.cmd1, extended=pkt.extended, bcast=pkt.bcast):
            return
        kind = ("bcast " if pkt.bcast else "") + ("ext" if pkt.extended else "std")
        key = (kind, pkt.cmd1)
        unknown[key] += 1
        if unknown[key] == 1:
            log.warning("unnamed command: %s cmd1=0x%02X from %s (%s)",
                        kind, pkt.cmd1, pkt.from_addr or pkt.to_addr, pkt.summary())

    def emit(recs: list[dict[str, Any]]) -> None:
        nonlocal seen
        for rec in recs:
            seen += 1
            if writer is not None:
                writer.write(rec)
            if mqtt is not None:
                mqtt.publish(rec)
            if not a.quiet:
                print(json.dumps(rec, separators=(",", ":")), flush=True)

    capture_seq = 0
    # Liveness is a *decoded packet*, not a block: a deaf dongle still
    # false-syncs on noise about once a minute and hands over junk, which
    # would keep a block-based watchdog quiet indefinitely (seen 2026-09-19).
    # The decoder runs on the worker thread, so it is the worker that moves
    # this forward; the receive loop only reads it.
    live = _Liveness()
    last_action = time.time()
    rearms = heals = 0
    if a.mesh_capture:
        if mqtt is None:
            p.error("--mesh-capture needs --mqtt")
        log.info("publishing raw captures as mesh receiver %r", a.mesh_capture)

    def process(item: tuple[Any, ...]) -> None:
        """Everything that happens to one block. Runs on the worker thread.

        Publishing a capture, decoding with repair, the all-on watcher and
        the log write together take far longer than a block takes to arrive
        during a burst. Doing them between reads is what starved the dongle:
        when the CC1111's USB IN buffer is not emptied promptly its firmware
        drops the packet *without re-arming DMA* and the receiver then hands
        over nothing at all until something re-arms it.
        """
        nonlocal capture_seq
        ts, bits, soft, header_index, snr_db, rssi = item
        if a.mesh_capture and mqtt is not None:
            capture_seq += 1
            mqtt.publish_capture(bits, receiver=a.mesh_capture, timestamp=ts,
                                 rssi_dbm=rssi, seq=capture_seq, soft=soft,
                                 snr_db=snr_db)
        packets = _decode(bits, ts, repair=not a.no_repair, show_all=a.all,
                          soft=soft, header_index=header_index, tracker=tracker)
        if any(q.calc_crc is not None for q in packets):
            live.last_packet = time.time()
            live.junk_blocks = 0
        else:
            live.junk_blocks += 1
        for pkt in packets:
            pkt.rssi_dbm = rssi
            if a.unknown_commands:
                note_unknown(pkt)
            if watcher is not None:
                for trig in watcher.observe(pkt, bits=bits, rssi_dbm=rssi,
                                            snr_db=pkt.snr_db):
                    log.warning("%s", watcher.report(trig))
                    if mqtt is not None:
                        mqtt.publish_alert({
                            "alert": trig.kind, "at": trig.at,
                            "detail": trig.detail,
                            "report": watcher.report(trig),
                            "packet": trig.packet.to_dict()
                            if trig.packet else None})
            if dd is None:
                emit([pkt.to_dict()])
            else:
                dd.add(pkt)

    work: queue.Queue[tuple[Any, ...] | None] = queue.Queue(maxsize=max(1, a.queue))

    def worker() -> None:
        while True:
            try:
                item = work.get(timeout=0.5)
            except queue.Empty:
                # Deduped records have to age out on time even while the air
                # is silent, so the idle tick still has to happen.
                if dd is not None:
                    emit(dd.pop_ready())
                continue
            try:
                if item is None:
                    return
                process(item)
            except Exception:
                # One bad block must not take the receiver down with it.
                log.exception("decoding a block failed")
            finally:
                work.task_done()
            if dd is not None:
                # Hand over the records whose window has closed — they carry
                # the final repeats/hops_seen.
                emit(dd.pop_ready())

    decoder = threading.Thread(target=worker, name="insteon-rf-decode", daemon=True)
    decoder.start()

    try:
        with _open_radio(a) as radio:
            radio.configure_rx(sync_header=not a.carrier)
            log.info("monitoring on %s", getattr(radio, "name", a.backend))
            lossless = bool(getattr(radio, "lossless", False))

            def diag(when: str) -> dict[str, Any]:
                # What the radio itself says it is doing. Not logged on every
                # re-arm any more: at a threshold of seconds that would be the
                # whole log, and the register reads are not free either.
                if not hasattr(radio, "diagnostics"):
                    return {}
                state: dict[str, Any] = radio.diagnostics()
                log.warning("radio %s: %s", when, state)
                return state

            def heal_now(why: str) -> None:
                nonlocal heals
                log.warning("%s; healing the radio", why)
                try:
                    radio.heal()
                except Exception as err:
                    # Leave the loop alive: the next silence tick tries again.
                    log.error("heal failed: %r", err)
                heals += 1
                live.last_packet = time.time()
                diag("after heal")

            last_diag = 0.0
            diag("at start")
            while not STOP.is_set():
                if DIAG.is_set():
                    DIAG.clear()
                    diag(f"on request ({live.junk_blocks} undecodable block(s) "
                         "since the last packet)")
                got = _receive(radio, a.timeout)
                now = time.time()
                quiet = now - live.last_packet
                if (_silence_action(quiet, a.max_silence) == "rearm"
                        and now - last_action >= a.max_silence):
                    last_action = now
                    state: dict[str, Any] = {}
                    if quiet >= a.diag_after and now - last_diag >= a.diag_after:
                        last_diag = now
                        state = diag(f"after {quiet / 60.0:.0f} min without a packet "
                                     f"({live.junk_blocks} undecodable block(s) meanwhile)")
                    if state.get("answering") is False and hasattr(radio, "heal"):
                        # A dongle that will not answer a register read will
                        # not take a mode change either. This, and repeated
                        # USB errors in RfcatRadio.receive, are the only two
                        # things a USB reset is for.
                        heal_now(f"nothing received for {quiet / 60.0:.0f} min "
                                 "and the dongle is not answering")
                    else:
                        try:
                            radio.configure_rx(sync_header=not a.carrier)
                            rearms += 1
                            log.debug("re-armed receive after %.0fs of silence", quiet)
                        except Exception as err:
                            if hasattr(radio, "heal"):
                                heal_now(f"re-arm failed ({err!r})")
                            else:
                                log.error("re-arm failed: %r", err)
                if got is not None:
                    ts, bits, soft, header_index, snr_db = got
                    rssi = None
                    if not a.no_rssi and hasattr(radio, "read_rssi"):
                        rssi = radio.read_rssi()
                    item = (ts, bits, soft, header_index, snr_db, rssi)
                    if lossless:
                        # Replay: waiting is free and the same file must
                        # always decode to the same packets.
                        work.put(item)
                    else:
                        try:
                            work.put_nowait(item)
                        except queue.Full:
                            # Never block on a live radio: a stalled receive
                            # thread is the fault this queue exists to
                            # prevent, and a dropped block costs one message
                            # where stalling costs the next spell of them.
                            live.dropped += 1
                            if live.dropped == 1 or live.dropped % 100 == 0:
                                log.warning("decode queue full, dropped %d block(s) — "
                                            "raise --queue", live.dropped)
                if getattr(radio, "exhausted", False):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        # Stop the worker before the final flush so nothing writes behind it.
        # Never block on a full queue here: the point is to shut down.
        try:
            work.put(None, timeout=5.0)
        except queue.Full:
            log.warning("decode queue still full at shutdown; %d block(s) dropped",
                        work.qsize())
        decoder.join(timeout=10.0)
        if decoder.is_alive():
            log.warning("the decode thread did not stop; %d block(s) unprocessed",
                        work.qsize())
        if dd is not None:
            emit(dd.flush())
        if writer is not None:
            writer.close()
        if mqtt is not None:
            log.info("mqtt: %d packet(s), %d alert(s), %d capture(s) published",
                     mqtt.published, mqtt.alerts, mqtt.captures)
            mqtt.close()
        if rearms or heals:
            # Re-arms are routine now and cheap; heals are not, and a
            # dropped block means the decoder could not keep up.
            log.warning("silence watchdog: %d re-arm(s), %d heal(s)%s", rearms, heals,
                        f", {live.dropped} block(s) dropped" if live.dropped else "")
        if watcher is not None:
            log.warning("all-on watch: %s", watcher.summary())
        if unknown:
            log.warning("%d unnamed command(s) seen:", len(unknown))
            for (kind, cmd1), n in unknown.most_common():
                log.warning("    %-10s cmd1=0x%02X  %d time(s)", kind, cmd1, n)
    log.info("logged %d packet(s)", seen)
    return 0


# --------------------------------------------------------------------------- allon


def allon_main(argv: list[str] | None = None) -> int:
    """`monitor` preset for hunting phantom all-on events."""
    argv = list(argv or [])
    if not any(x.startswith("--dump-dir") for x in argv):
        argv += ["--dump-dir", "allon-dumps"]
    if not any(x in ("-o", "--out") for x in argv):
        argv += ["-o", "allon.jsonl"]
    print("watching for phantom 'all on' triggers — a group-0 all-link broadcast is the\n"
          "classic culprit. Leave this running; Ctrl-C for a summary.\n"
          "Context and attribution go to the dump directory on each trigger.\n",
          file=sys.stderr)
    return monitor_main(["--watch-all-on", "--unknown-commands", *argv])


# --------------------------------------------------------------------------- reset


def mesh_main(argv: list[str] | None = None) -> int:
    """Run the listener mesh: captures in, miss table out, injection optional."""
    from .fusion import Fusion
    from .inject import Injector, Tier, load_battery_addrs
    from .mesh import PLM_INJECT_TOPIC, PLM_RX_TOPIC, MeshService, PlmLink
    from .monitor import JsonlWriter
    from .radio.mqtt import DEFAULT_PREFIX, MqttReceiver

    p = argparse.ArgumentParser(
        prog="insteon-rf mesh",
        description="Fuse captures from the ESPHome listener boards, compare them against what "
                    "the PLM heard, and optionally hand the difference to insteon-mqtt.",
        epilog="Injection is OFF unless --inject names at least one tier, and even then runs in "
               "shadow mode until --live. Nothing here transmits: the PLM owns the air.")
    p.add_argument("--mqtt", metavar="HOST[:PORT]", required=True,
                   help="MQTT broker carrying both the board captures and insteon/raw/*")
    p.add_argument("--prefix", default=DEFAULT_PREFIX,
                   help="board capture topic prefix, subscribed as <prefix>/rx/+ "
                        "(default %(default)s)")
    p.add_argument("--mqtt-user", default=os.environ.get("INSTEONRF_MQTT_USER"),
                   help="MQTT username (default: $INSTEONRF_MQTT_USER)")
    p.add_argument("--mqtt-pass", default=os.environ.get("INSTEONRF_MQTT_PASS"),
                   help="MQTT password (default: $INSTEONRF_MQTT_PASS — prefer this to a "
                        "command line, which is visible in ps)")
    p.add_argument("-o", "--out", metavar="FILE", help="append fused events as JSON lines")
    p.add_argument("--max-bytes", type=int, default=32 << 20, help="rotate FILE at this size")
    p.add_argument("--backups", type=int, default=5, help="how many rotated files to keep")

    p.add_argument("--plm-topic", default=PLM_RX_TOPIC,
                   help="topic where the patched insteon-mqtt mirrors what the modem read "
                        "(default %(default)s); this is how suppression knows what the PLM "
                        "already heard")
    p.add_argument("--inject-topic", default=PLM_INJECT_TOPIC,
                   help="topic insteon-mqtt accepts injections on (default %(default)s)")
    p.add_argument("--no-plm", action="store_true",
                   help="do not watch the PLM mirror at all (disables injection: without it "
                        "there is no way to know what the modem already heard)")

    p.add_argument("--inject", metavar="TIER", action="append", default=[],
                   choices=[t.name.lower() for t in Tier],
                   help="enable an injection tier; repeatable. battery = broadcasts from "
                        "RF-only battery devices (safest, and they cannot be polled), "
                        "group = group broadcasts and cleanup reports, state = unsolicited "
                        "direct messages. ACKs and NAKs are never injected.")
    p.add_argument("--live", action="store_true",
                   help="actually publish injections (default is shadow mode: decide and log, "
                        "publish nothing)")
    p.add_argument("--plm-addr", default="2B.93.07",
                   help="the modem's address, so its own transmissions are never injected back "
                        "at it (default %(default)s)")
    p.add_argument("--battery-addrs", metavar="FILE",
                   help="one address per line: devices with no powerline path, which cannot be "
                        "polled and so can only be helped by injection")
    p.add_argument("--per-device-interval", type=float, default=30.0,
                   help="seconds between injections naming one device (default %(default)s)")
    p.add_argument("--global-per-minute", type=int, default=6,
                   help="ceiling on injections per minute; inbound messages delay the modem's "
                        "next transmit, so a flood would stall outbound commands "
                        "(default %(default)s)")
    p.add_argument("--no-combine", action="store_true",
                   help="do not try to recover a packet by voting across receivers")

    p.add_argument("--window", type=float, default=0.6,
                   help="seconds to hold a message open for further copies (default %(default)s)")
    p.add_argument("--report-every", type=float, default=900.0,
                   help="seconds between miss-table reports, 0 to disable (default %(default)s)")
    p.add_argument("--report-file", metavar="FILE",
                   help="also write the miss table here as JSON on each report")
    p.add_argument("--quiet", action="store_true", help="do not echo events to stdout")
    _log_args(p)
    a = p.parse_args(argv)
    _setup_logging(a.verbose or 1)
    _install_signal_handlers()

    host, _, port = a.mqtt.partition(":")
    port_n = int(port or 1883)

    tiers = [Tier[t.upper()] for t in a.inject]
    if tiers and a.no_plm:
        p.error("--inject needs the PLM mirror; drop --no-plm")

    receiver = MqttReceiver(host, port_n, prefix=a.prefix,
                            username=a.mqtt_user, password=a.mqtt_pass)
    plm = None if a.no_plm else PlmLink(host, port_n, username=a.mqtt_user,
                                        password=a.mqtt_pass, rx_topic=a.plm_topic,
                                        inject_topic=a.inject_topic)
    battery = load_battery_addrs(a.battery_addrs) if a.battery_addrs else set()
    injector = None
    if tiers:
        injector = Injector(
            publish=plm.publish_inject if plm is not None else None,
            plm_addr=a.plm_addr, battery_addrs=battery, allow=tiers,
            shadow=not a.live, per_device_interval_s=a.per_device_interval,
            global_per_minute=a.global_per_minute)
        log.info("injection tiers %s, %s", [t.name for t in tiers],
                 "LIVE" if a.live else "shadow mode")
    elif a.live:
        log.warning("--live has no effect without --inject")

    writer = JsonlWriter(a.out, max_bytes=a.max_bytes, backups=a.backups) if a.out else None
    fusion = Fusion(a.window, allow_combine=not a.no_combine)

    def echo(event: Any) -> None:
        if a.quiet:
            return
        who = event.closest or "?"
        flag = "" if event.plm_saw_it is not False else "  PLM MISSED"
        print(f"{_ts_line(event.first_seen, 0).split()[0]} {event.packet.summary()}  "
              f"[{event.heard_by} rx, closest {who}]{flag}", flush=True)

    # plm_addr matters even with no injector: without it the miss table
    # counts the modem's own transmissions, which are heard on RF but never
    # come back as inbound messages, so the modem leads its own report at a
    # 100% miss rate and every real device is buried under it.
    service = MeshService(receiver, injector=injector, plm=plm, fusion=fusion,
                          writer=writer, require_plm_link=not a.no_plm,
                          on_event=echo, plm_addr=a.plm_addr)

    if battery:
        log.info("%d battery devices treated as unpollable", len(battery))
    log.info("mesh listening; ctrl-c to stop")

    next_report = time.time() + a.report_every if a.report_every else None
    try:
        while not STOP.is_set():
            cap = receiver.next_capture(500)
            if cap is not None:
                service.handle_capture(cap)
            service.drain()
            if next_report is not None and time.time() >= next_report:
                next_report = time.time() + a.report_every
                print(service.misses.report(), flush=True)
                log.info("stats %s", json.dumps(service.stats(), separators=(",", ":")))
                if a.report_file:
                    with open(a.report_file, "w", encoding="utf-8") as fh:
                        json.dump({"misses": service.misses.to_dict(),
                                   "stats": service.stats()}, fh, indent=2)
    finally:
        for event in fusion.flush():
            service.misses.note(event)
            if writer is not None:
                writer.write(event.to_dict())
        print(service.misses.report(), flush=True)
        log.info("final stats %s", json.dumps(service.stats(), separators=(",", ":")))
        if writer is not None:
            writer.close()
        receiver.close()
        if plm is not None:
            plm.close()
    return 0


def reset_main(argv: list[str] | None = None) -> int:
    from .radio import USB_PID, USB_VID, usb_reset

    p = argparse.ArgumentParser(prog="insteon-rf reset", description="USB-reset a wedged rfcat dongle.")
    p.add_argument("--vid", type=lambda s: int(s, 0), default=USB_VID)
    p.add_argument("--pid", type=lambda s: int(s, 0), default=USB_PID)
    p.add_argument("--timeout", type=float, default=10.0,
                   help="give up waiting for the reset after this many seconds")
    _log_args(p)
    a = p.parse_args(argv)
    _setup_logging(max(a.verbose, 1))

    if not usb_reset(a.vid, a.pid, a.timeout):
        return 1
    print("reset sent; give the dongle a few seconds")
    return 0


# --------------------------------------------------------------------------- dispatcher

COMMANDS = {
    "recv": (recv_main, "receive raw bit strings from a radio"),
    "send": (send_main, "transmit bit strings with a radio"),
    "print": (print_main, "decode bit strings into packets"),
    "pkt": (pkt_main, "build a packet as a bit string"),
    "dump": (dump_main, "verbose frame-level dump of bit strings"),
    "monitor": (monitor_main, "log all RF traffic as JSON lines (and to MQTT)"),
    "modulate": (modulate_main, "bit strings -> raw I/Q for an SDR transmitter"),
    "demod": (demod_main, "raw I/Q -> bit strings"),
    "clip": (clip_main, "split raw I/Q into one file per burst"),
    "allon": (allon_main, "hunt phantom 'all on' events and pin them on a device"),
    "mesh": (mesh_main, "fuse listener-board captures and feed insteon-mqtt"),
    "reset": (reset_main, "USB-reset a wedged rfcat dongle"),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: insteon-rf <command> [options]\n\ncommands:")
        for name, (_, help_) in COMMANDS.items():
            print(f"  {name:9s} {help_}")
        print("\nRun 'insteon-rf <command> -h' for details.")
        return 0 if argv else 2
    if argv[0] in ("-V", "--version"):
        from . import __version__

        print(f"insteon-rf {__version__}")
        return 0
    cmd = COMMANDS.get(argv[0])
    if cmd is None:
        print(f"insteon-rf: unknown command {argv[0]!r}", file=sys.stderr)
        return 2
    try:
        return cmd[0](argv[1:])
    except DongleError as err:
        # Running under a supervisor (k8s restartPolicy, systemd): report the
        # problem in one line and let the supervisor retry, rather than dumping
        # a traceback. Happens routinely when a previous process still holds
        # the USB interface for a moment.
        log.error("%s", err)
        return 1
    except BrokenPipeError:
        # Downstream closed the pipe ('... | head'), which is normal for a
        # pipeline tool. Let the exception unwind first so the radio is
        # released, then point stdout at /dev/null so interpreter shutdown
        # does not complain about the failed flush.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:  # pragma: no cover - stdout may already be gone
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
