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
        p.add_argument("--demod", choices=("auto", "c", "numpy"), default="auto",
                       help="SDR backends: demodulator to use (default %(default)s)")
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


def _receive(radio: Any, timeout_ms: int) -> tuple[float, str, Any, int | None] | None:
    """One burst from any backend, keeping soft decisions where they exist.

    A hardware demodulator (the rfcat dongle) can only give hard bits; the
    numpy SDR path also returns per-symbol confidence and the located frame
    grid, which :func:`_decode` puts to work.
    """
    if hasattr(radio, "receive_burst"):
        burst = radio.receive_burst(timeout_ms)
        if burst is None:
            return None
        return (burst.timestamp or time.time(), burst.bits, burst.soft, burst.header_index)
    got = radio.receive_bits(timeout_ms)
    if got is None:
        return None
    return got[0], got[1], None, None


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
                ts, bits, soft, header_index = got
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

    try:
        with _open_radio(a) as radio:
            radio.configure_rx(sync_header=not a.carrier)
            log.info("monitoring on %s", getattr(radio, "name", a.backend))
            while not STOP.is_set():
                got = _receive(radio, a.timeout)
                if got is not None:
                    ts, bits, soft, header_index = got
                    rssi = None
                    if not a.no_rssi and hasattr(radio, "read_rssi"):
                        rssi = radio.read_rssi()
                    for pkt in _decode(bits, ts, repair=not a.no_repair, show_all=a.all,
                                       soft=soft, header_index=header_index, tracker=tracker):
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
                # Every tick, hand over the records whose window has closed —
                # they carry the final repeats/hops_seen.
                if dd is not None:
                    emit(dd.pop_ready())
                if getattr(radio, "exhausted", False):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if dd is not None:
            emit(dd.flush())
        if writer is not None:
            writer.close()
        if mqtt is not None:
            log.info("mqtt: %d packet(s), %d alert(s) published", mqtt.published, mqtt.alerts)
            mqtt.close()
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
