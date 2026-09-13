#!/usr/bin/env python3
"""Stress the rfcat USB path, looking for the wedge described in PLAN.md §2.

Three scenarios, each reporting how many cycles ran and what failed:

  cycles   open / configure / receive / close, N times in this process
  kill     the same, in a child process killed with SIGTERM mid-receive
  race     two processes opening the dongle at once

Usage:
    python tools/usb_stress.py cycles --count 200 --recv 2
    python tools/usb_stress.py kill   --count 50
    python tools/usb_stress.py race   --count 20
    python tools/usb_stress.py all

Nothing is transmitted. Not shipped as part of the package.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from insteonrf.radio import DongleError, RfcatRadio, usb_reset  # noqa: E402

log = logging.getLogger("stress")


def one_cycle(recv_s: float, *, auto_reset: bool, carrier: bool = False) -> int:
    """Open, configure, receive for ``recv_s``, close. Returns blocks received."""
    blocks = 0
    with RfcatRadio(auto_reset=auto_reset) as radio:
        radio.configure_rx(sync_header=not carrier)
        end = time.monotonic() + recv_s
        while (left := end - time.monotonic()) > 0:
            if radio.receive_bits(max(1, int(left * 1000))) is not None:
                blocks += 1
    return blocks


def run_cycles(args: argparse.Namespace) -> int:
    failures = []
    heals = 0
    blocks = 0
    t0 = time.monotonic()
    for n in range(1, args.count + 1):
        try:
            blocks += one_cycle(args.recv, auto_reset=not args.no_auto_reset)
        except DongleError as err:
            failures.append((n, repr(err)))
            log.error("cycle %d: %s", n, err)
            usb_reset()
            time.sleep(3)
        except Exception as err:  # noqa: BLE001 - the point is to catalogue these
            failures.append((n, repr(err)))
            log.error("cycle %d: %r", n, err)
        if n % 10 == 0:
            log.info("cycle %d/%d  blocks=%d failures=%d  %.1fs elapsed",
                     n, args.count, blocks, len(failures), time.monotonic() - t0)
    print(f"\ncycles: {args.count} run, {len(failures)} failures, {blocks} blocks received, "
          f"{heals} self-heals, {time.monotonic() - t0:.0f}s")
    for n, err in failures:
        print(f"  cycle {n}: {err}")
    return 1 if failures else 0


CHILD = """
import sys, time
sys.path.insert(0, {root!r})
from insteonrf.radio import RfcatRadio
with RfcatRadio() as radio:
    radio.configure_rx()
    print("ready", flush=True)
    end = time.monotonic() + 30
    while time.monotonic() < end:
        radio.receive_bits(500)
"""


def _child_argv() -> list[str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-c", CHILD.format(root=root)]


def run_kill(args: argparse.Namespace) -> int:
    """Kill a receiver mid-transfer, then check the next open still works."""
    failures = []
    for n in range(1, args.count + 1):
        proc = subprocess.Popen(_child_argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        # wait until it is actually receiving, then kill at a random moment
        ready = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if line.startswith("ready"):
                ready = True
                break
            if proc.poll() is not None:
                break
        if not ready:
            why = proc.stderr.read()[-200:] if proc.stderr else ""
            failures.append((n, f"child never became ready: {why}"))
            proc.kill()
            continue
        time.sleep(random.uniform(0.05, 1.5))
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            failures.append((n, "child ignored SIGTERM"))
            proc.kill()
            proc.wait()
        # the dongle must be usable again straight away
        try:
            one_cycle(1.0, auto_reset=not args.no_auto_reset)
        except Exception as err:  # noqa: BLE001
            failures.append((n, f"reopen after kill failed: {err!r}"))
            log.error("cycle %d: reopen failed: %r", n, err)
        log.info("kill cycle %d/%d failures=%d", n, args.count, len(failures))
    print(f"\nkill: {args.count} run, {len(failures)} failures")
    for n, err in failures:
        print(f"  cycle {n}: {err}")
    return 1 if failures else 0


def run_race(args: argparse.Namespace) -> int:
    """Two processes opening the dongle at once — one must lose cleanly."""
    failures = []
    for n in range(1, args.count + 1):
        a = subprocess.Popen(_child_argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        b = subprocess.Popen(_child_argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(6)
        for proc in (a, b):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                failures.append((n, "racer ignored SIGTERM"))
                proc.kill()
        try:
            one_cycle(1.0, auto_reset=not args.no_auto_reset)
        except Exception as err:  # noqa: BLE001
            failures.append((n, f"reopen after race failed: {err!r}"))
            log.error("race %d: reopen failed: %r", n, err)
        log.info("race cycle %d/%d failures=%d", n, args.count, len(failures))
    print(f"\nrace: {args.count} run, {len(failures)} failures")
    for n, err in failures:
        print(f"  cycle {n}: {err}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", choices=("cycles", "kill", "race", "all"))
    p.add_argument("--count", type=int, default=0, help="iterations (default: per-scenario)")
    p.add_argument("--recv", type=float, default=2.0, help="seconds to receive per cycle")
    p.add_argument("--no-auto-reset", action="store_true", help="disable the self-healing watchdog")
    p.add_argument("-v", "--verbose", action="count", default=1)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose < 2 else logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(message)s")

    defaults = {"cycles": 200, "kill": 50, "race": 20}
    rc = 0
    for scenario in (("cycles", "kill", "race") if a.scenario == "all" else (a.scenario,)):
        a.count = a.count or defaults[scenario]
        rc |= {"cycles": run_cycles, "kill": run_kill, "race": run_race}[scenario](a)
        a.count = 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
