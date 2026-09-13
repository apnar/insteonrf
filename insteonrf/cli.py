"""Command-line tools. Every stage reads/writes ASCII bit strings (one packet
burst per line; ``#`` lines are metadata) so they can be piped together::

    insteon-rf recv | insteon-rf print
    insteon-rf pkt -s 13.25.80 -d 16.3F.E5 13 00 | insteon-rf send
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from typing import Iterable, TextIO

from .packet import Packet, dump_frames, iter_bit_lines, parse_bits
from .radio import DEFAULT_DRATE, DEFAULT_FREQ, Radio

# --------------------------------------------------------------------------- helpers


def _radio_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-f", "--freq", type=int, default=DEFAULT_FREQ, help="carrier frequency in Hz (default %(default)s)")
    p.add_argument("-B", "--baud", type=int, default=DEFAULT_DRATE, help="data rate in baud (default %(default)s)")
    p.add_argument("--index", type=int, default=0, help="rfcat device index when several dongles are attached")


def _open_lines(paths: list[str]) -> Iterable[str]:
    if not paths or paths == ["-"]:
        yield from sys.stdin
        return
    for path in paths:
        with open(path) as fh:
            yield from fh


def _print_packets(packets: list[Packet], out: TextIO, *, verbose: bool = False,
                   show_time: bool = False, log: TextIO | None = None) -> None:
    for p in packets:
        if show_time:
            out.write(p.time_str + "  ")
        out.write((p.describe() if verbose else p.summary()) + "\n")
        if log is not None:
            log.write(p.hex_line() + "\n")
    out.flush()
    if log is not None:
        log.flush()


def _ts_line(ts: float, nbits: int) -> str:
    t = _dt.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")
    return f"# {t} len={nbits // 8}"


# --------------------------------------------------------------------------- recv


def recv_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf recv",
                                description="Receive Insteon RF with an rfcat dongle and print raw bit strings.")
    _radio_args(p)
    p.add_argument("-t", "--time", action="store_true", help="emit a '# <timestamp> len=N' line before each burst")
    p.add_argument("-D", "--decode", action="store_true", help="decode packets here instead of printing raw bits")
    p.add_argument("-v", "--verbose", action="store_true", help="with -D: multi-line decode; otherwise print radio config")
    p.add_argument("-a", "--all", action="store_true", help="with -D: also show fragments shorter than a full packet")
    p.add_argument("--carrier", action="store_true",
                   help="capture on carrier detect alone instead of syncing on the Insteon start header "
                        "(noisier, but shows bursts the sync word misses)")
    p.add_argument("--timeout", type=int, default=2000, help="USB receive timeout in ms (default %(default)s)")
    a = p.parse_args(argv)

    with Radio(a.freq, a.baud, index=a.index) as radio:
        radio.configure_rx(sync_header=not a.carrier)
        if a.verbose and not a.decode:
            radio.print_config()
        try:
            for ts, bits in radio.iter_bits(a.timeout):
                if a.decode:
                    pkts = parse_bits(bits, ts)
                    if not a.all:
                        pkts = [q for q in pkts if q.calc_crc is not None]
                    _print_packets(pkts, sys.stdout, verbose=a.verbose, show_time=a.time)
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
                                description="Transmit bit strings (from 'insteon-rf pkt') with an rfcat dongle.")
    _radio_args(p)
    p.add_argument("-r", "--repeat", type=int, default=1, help="send each line this many times (default 1)")
    p.add_argument("--gap-ms", type=int, default=60, help="gap between repeats in ms (default %(default)s)")
    p.add_argument("-i", "--invert", action="store_true", help="invert bits before sending")
    p.add_argument("-L", "--listen", type=int, default=0, metavar="MS",
                   help="after each transmission, receive for MS milliseconds and print decoded replies")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("files", nargs="*", help="bit-string files (default: stdin)")
    a = p.parse_args(argv)

    sent = 0
    with Radio(a.freq, a.baud, index=a.index) as radio:
        for kind, line in iter_bit_lines(_open_lines(a.files)):
            if kind != "bits":
                continue
            if a.verbose:
                for q in parse_bits(line):
                    print("tx:", q.summary(), file=sys.stderr)
            radio.configure_tx()
            radio.transmit_bits(line, repeat=a.repeat, gap_s=a.gap_ms / 1000, invert=a.invert)
            sent += 1
            if a.listen:
                radio.configure_rx(sync_header=True)
                end = time.monotonic() + a.listen / 1000
                while (left := end - time.monotonic()) > 0:
                    r = radio.receive_bits(max(1, int(left * 1000)))
                    if r is not None:
                        pkts = [q for q in parse_bits(r[1], r[0]) if q.calc_crc is not None]
                        _print_packets(pkts, sys.stdout, show_time=True)
    if a.verbose:
        print(f"sent {sent} packet(s)", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- print


def print_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf print",
                                description="Decode Insteon packets from bit strings on stdin (or files).")
    p.add_argument("-v", "--verbose", action="store_true", help="multi-line decode with addresses and command names")
    p.add_argument("-t", "--time", action="store_true", help="prefix each packet with the time it was decoded")
    p.add_argument("-a", "--all", action="store_true", help="also show fragments too short to have a CRC")
    p.add_argument("-l", "--log", metavar="FILE", help="append the hex line of every packet to FILE")
    p.add_argument("files", nargs="*", help="input files (default: stdin)")
    a = p.parse_args(argv)

    log = open(a.log, "a") if a.log else None
    try:
        for kind, line in iter_bit_lines(_open_lines(a.files)):
            if kind == "meta":
                print(line)
                continue
            if kind == "junk":
                print(f"skipping non-bit line: {line[:40]!r}", file=sys.stderr)
                continue
            pkts = parse_bits(line)
            if not a.all:
                pkts = [q for q in pkts if q.calc_crc is not None]
            _print_packets(pkts, sys.stdout, verbose=a.verbose, show_time=a.time, log=log)
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()
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
            if add_crc:
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
    p.add_argument("-s", "--src", help="source address (e.g. 13.25.80) — must be linked to the target for i2cs devices")
    p.add_argument("-d", "--dst", help="destination address")
    p.add_argument("-g", "--group", type=lambda s: int(s, 0), help="all-link group number (instead of -d)")
    p.add_argument("-e", "--extended", action="store_true", help="extended (14-byte payload) packet")
    p.add_argument("-b", "--broadcast", action="store_true", help="set the broadcast flag")
    p.add_argument("-a", "--ack", dest="ack", action="store_true", default=None, help="set the ACK flag")
    p.add_argument("--no-ack", dest="ack", action="store_false", help="clear the ACK flag (raw packets)")
    p.add_argument("-m", "--max-hops", type=int, choices=range(4), help="max hops (default 3)")
    p.add_argument("-l", "--hops-left", type=int, choices=range(4), help="hops left (default 3)")
    p.add_argument("-c", "--count", type=int, default=1, help="emit the packet this many times, one per line")
    p.add_argument("-I", "--no-invert", action="store_true", help="emit logical instead of on-air (inverted) bits")
    p.add_argument("-p", "--pad", action="store_true", help="append pad bytes to a raw packet")
    p.add_argument("-r", "--raw", action="store_true", help="words are raw wire bytes in hex (':' separators ignored)")
    p.add_argument("-n", "--dry-run", action="store_true", help="describe the packet on stderr, print nothing")
    p.add_argument("-v", "--verbose", action="store_true", help="describe the packet on stderr as well")
    p.add_argument("words", nargs="*", help="cmd1 [cmd2] [ext data...] in hex, or raw bytes with -r")
    a = p.parse_args(argv)

    pkt = build_from_args(a)
    if a.verbose or a.dry_run:
        print(pkt.describe(), file=sys.stderr)
        print(f"{len(pkt.data)} bytes -> {len(pkt.to_bits())} bits on air", file=sys.stderr)
    if a.dry_run:
        return 0
    bits = pkt.to_bits(invert=not a.no_invert)
    for _ in range(a.count):
        print(bits)
    return 0


# --------------------------------------------------------------------------- dump


def dump_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf dump",
                                description="Frame-by-frame breakdown of bit strings, for debugging a demodulator.")
    p.add_argument("files", nargs="*", help="input files (default: stdin)")
    a = p.parse_args(argv)
    for kind, line in iter_bit_lines(_open_lines(a.files)):
        if kind == "bits":
            print(dump_frames(line))
            print()
    return 0


# --------------------------------------------------------------------------- dispatcher

COMMANDS = {
    "recv": (recv_main, "receive raw bit strings from an rfcat dongle"),
    "send": (send_main, "transmit bit strings with an rfcat dongle"),
    "print": (print_main, "decode bit strings into packets"),
    "pkt": (pkt_main, "build a packet as a bit string"),
    "dump": (dump_main, "verbose frame-level dump of bit strings"),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: insteon-rf <command> [options]\n\ncommands:")
        for name, (_, help_) in COMMANDS.items():
            print(f"  {name:8s} {help_}")
        print("\nRun 'insteon-rf <command> -h' for details.")
        return 0 if argv else 2
    cmd = COMMANDS.get(argv[0])
    if cmd is None:
        print(f"insteon-rf: unknown command {argv[0]!r}", file=sys.stderr)
        return 2
    return cmd[0](argv[1:])



# --------------------------------------------------------------------------- reset


def reset_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insteon-rf reset", description="USB-reset a wedged rfcat dongle.")
    p.add_argument("--vid", type=lambda s: int(s, 0), default=0x1D50)
    p.add_argument("--pid", type=lambda s: int(s, 0), default=0x6048)
    a = p.parse_args(argv)
    import usb.core  # pyusb, an rfcat dependency

    dev = usb.core.find(idVendor=a.vid, idProduct=a.pid)
    if dev is None:
        print(f"no USB device {a.vid:04x}:{a.pid:04x} found", file=sys.stderr)
        return 1
    try:
        dev.reset()
    except usb.core.USBError as err:
        # A timeout here is common; the device usually comes back anyway.
        print(f"reset: {err}", file=sys.stderr)
    print("reset sent; give the dongle a few seconds")
    return 0


COMMANDS["reset"] = (reset_main, "USB-reset a wedged rfcat dongle")


if __name__ == "__main__":
    sys.exit(main())
