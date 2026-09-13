#!/usr/bin/env python3
"""Measure demodulator sensitivity: how much noise each chain survives.

Adds white Gaussian noise to modulated packets and reports the fraction
recovered (CRC valid, bytes identical) per detector, plus the mean estimated
SNR. Use it to justify changes rather than guess at them.

    python tools/dsp_bench.py                 # full sweep, all detectors
    python tools/dsp_bench.py --trials 40     # tighter error bars
    python tools/dsp_bench.py --sigma 60      # one noise level
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from insteonrf import dsp  # noqa: E402
from insteonrf.packet import Packet, parse_bits  # noqa: E402
from insteonrf.recover import recover_from_burst  # noqa: E402

STD = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F, cmd2=0x00)
EXT = Packet.build("2B.93.07", "29.4E.52", cmd1=0x2F, ext_data=[0, 0, 0x0F, 0xFF, 1, 2, 3])
DEMOD_C = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fsk2_demod")


def noisy(pkt: Packet, sigma: float, rng: np.random.Generator, *, amplitude: int = 100,
          cfo_hz: float = 0.0, rate_ppm: float = 0.0) -> np.ndarray:
    """Modulated packet plus AWGN, optionally with carrier and clock error."""
    baud = dsp.DEFAULT_BAUD * (1.0 + rate_ppm / 1e6)
    clean = dsp.modulate_fsk2(pkt.to_bits(), amplitude=amplitude, baud=baud).astype(np.float64)
    if cfo_hz:
        n = np.arange(clean.size // 2)
        rot = np.exp(2j * np.pi * cfo_hz * n / dsp.DEFAULT_SAMPLE_RATE)
        iq = (clean[0::2] + 1j * clean[1::2]) * rot
        clean = np.empty_like(clean)
        clean[0::2], clean[1::2] = iq.real, iq.imag
    return np.clip(np.rint(clean + rng.normal(0, sigma, clean.size)), -127, 127).astype(np.int8)


def recovered(pkt: Packet, result: list[str] | list[Packet]) -> bool:
    """True when the exact packet came back, whether as bit strings or packets."""
    for item in result:
        if isinstance(item, Packet):
            if item.data == pkt.data:
                return True
        else:
            if any(q.data == pkt.data and q.valid for q in parse_bits(item)):
                return True
    return False


def false_accepts(args: argparse.Namespace) -> int:
    """Feed pure noise and count packets each chain claims to have found.

    The packet CRC is only 8 bits, so a repair search that tries N candidates
    can be expected to accept roughly N/256 pieces of garbage unless something
    else constrains it — here, the frame-index check.
    """
    rng = np.random.default_rng(args.seed)
    n = int(0.06 * dsp.DEFAULT_SAMPLE_RATE) * 2  # ~60 ms of noise, one packet long
    print(f"false accepts over {args.trials} noise-only bursts (sigma {args.sigma}):")
    for name, fn in DETECTORS.items():
        if name.startswith("C "):
            continue
        hits = 0
        for _ in range(args.trials):
            noise = np.clip(np.rint(rng.normal(0, args.sigma, n)), -127, 127).astype(np.int8)
            result = fn(noise)
            for item in result:
                if isinstance(item, Packet):
                    hits += 1
                else:
                    hits += sum(1 for q in parse_bits(item) if q.valid)
        print(f"  {name:22} {hits:4d} accepted ({hits / args.trials:.2f} per burst)")
    return 0


def run_c(samples: np.ndarray) -> list[str]:
    if not os.path.exists(DEMOD_C):
        return []
    out = subprocess.run([DEMOD_C, "-U"], input=samples.tobytes(), capture_output=True)
    return [ln for ln in out.stdout.decode("ascii", "replace").splitlines() if ln[:1] in "01"]


def ml_recover(samples: np.ndarray, *, repair: bool) -> list[Packet]:
    """Full chain: ML detection then soft-decision framing."""
    out = []
    for burst in dsp.demodulate_bursts(samples):
        for rec in recover_from_burst(burst, repair=repair):
            out.append(rec.packet)
    return out


DETECTORS = {
    "C fsk2_demod": lambda s: run_c(s),
    "numpy discriminator": lambda s: dsp.demodulate_fsk2(s, method="discriminator"),
    "numpy ML": lambda s: dsp.demodulate_fsk2(s, method="ml"),
    "ML + soft frames": lambda s: ml_recover(s, repair=False),
    "ML + soft + repair": lambda s: ml_recover(s, repair=True),
}


def sweep(args: argparse.Namespace) -> int:
    """Sweep signal amplitude against a fixed noise floor, as a receiver sees it."""
    rng = np.random.default_rng(args.seed)
    pkt = {"std": STD, "ext": EXT}[args.packet]
    if args.amplitudes:
        amps = [int(a) for a in args.amplitudes.split(",")]
    elif args.amplitude:
        amps = [args.amplitude]
    else:
        amps = [100, 40, 20, 10, 6, 4, 3, 2]
    print(f"noise sigma {args.sigma}, {args.trials} trials per point, {args.packet} packet"
          f"{f', cfo {args.cfo} Hz' if args.cfo else ''}"
          f"{f', clock {args.rate_ppm} ppm' if args.rate_ppm else ''}\n")
    names = [n for n in DETECTORS if not args.detectors or n in args.detectors.split(",")]
    if not names:
        print(f"no detector matched {args.detectors!r}; known: {', '.join(DETECTORS)}")
        return 1
    print(f"{'ampl':>5} {'symSNR*':>8} " + " ".join(f"{n:>21}" for n in names))
    for amp in amps:
        # Approximate per-symbol SNR after integrating sps samples of a
        # constant-envelope tone; noncoherent 2-FSK needs ~13 dB of this for a
        # packet-error rate low enough to carry 13 bytes uncoded.
        ebn0 = 10 * np.log10((amp**2 / 2) / max(args.sigma**2, 1e-9)
                             * (dsp.DEFAULT_SAMPLE_RATE / dsp.DEFAULT_BAUD))
        # Paired trials: every detector sees the *same* noise realisations, so
        # the columns are comparable rather than each getting its own luck.
        ok = dict.fromkeys(names, 0)
        for _ in range(args.trials):
            samples = noisy(pkt, args.sigma, rng, amplitude=amp, cfo_hz=args.cfo,
                            rate_ppm=args.rate_ppm)
            for name in names:
                if recovered(pkt, DETECTORS[name](samples)):
                    ok[name] += 1
        # Report the binomial standard error with each point: near threshold a
        # 40-trial sample is worth about +-8%, which is enough to make two runs
        # look like they disagree when they do not.
        row = []
        for name in names:
            frac = ok[name] / args.trials
            se = 100 * (frac * (1 - frac) / args.trials) ** 0.5
            row.append(f"{100 * frac:14.0f}% ±{se:<4.0f}")
        print(f"{amp:>5} {ebn0:>7.1f}dB " + " ".join(row))
    return 0


def hard_bit_repair(args: argparse.Namespace) -> int:
    """How many symbol errors the hard-bit repair path survives.

    This is the rfcat dongle's situation: the CC1111 hands over hard decisions,
    so there is no soft information — but a flipped symbol still leaves an
    illegal Manchester pair, which marks exactly where to look.
    """
    from insteonrf.recover import recover_packets

    rng = np.random.default_rng(args.seed)
    pkt = {"std": STD, "ext": EXT}[args.packet]
    clean = pkt.to_bits()
    print(f"hard-bit recovery, {args.trials} trials per point, {args.packet} packet\n")
    print(f"{'symbol errors':>14} {'parse_bits':>12} {'+ repair':>12}")
    for nerr in range(0, 5):
        base = fixed = 0
        for _ in range(args.trials):
            bits = list(clean)
            for pos in rng.choice(len(bits), size=nerr, replace=False):
                bits[pos] = "1" if bits[pos] == "0" else "0"
            line = "".join(bits)
            if any(q.data == pkt.data and q.valid for q in parse_bits(line)):
                base += 1
            if any(r.packet.data == pkt.data for r in recover_packets(line)):
                fixed += 1
        print(f"{nerr:>14} {100 * base / args.trials:11.0f}% {100 * fixed / args.trials:11.0f}%")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trials", type=int, default=20)
    p.add_argument("--sigma", type=float, default=30.0, help="noise standard deviation")
    p.add_argument("--amplitude", type=int, default=0,
                   help="single signal amplitude instead of a sweep")
    p.add_argument("--amplitudes", default="", help="comma-separated amplitude list")
    p.add_argument("--detectors", default="",
                   help="comma-separated subset of: " + ", ".join(DETECTORS))
    p.add_argument("--packet", choices=("std", "ext"), default="std")
    p.add_argument("--cfo", type=float, default=0.0, help="carrier offset in Hz")
    p.add_argument("--rate-ppm", type=float, default=0.0, help="symbol clock error in ppm")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--false-accepts", action="store_true",
                   help="measure how often each chain accepts noise as a packet")
    p.add_argument("--hard-bits", action="store_true",
                   help="measure hard-bit repair (the rfcat dongle's case)")
    args = p.parse_args()
    if args.false_accepts:
        return false_accepts(args)
    if args.hard_bits:
        return hard_bit_repair(args)
    return sweep(args)


if __name__ == "__main__":
    sys.exit(main())
