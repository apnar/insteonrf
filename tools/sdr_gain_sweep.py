#!/usr/bin/env python3
"""Gain sweep for an RTL-SDR listener: record under identical test traffic, then score.

One recording per gain, each taken while the same batch of Get Engine Version
queries (benign, `0D`) goes to the same devices through insteon-mqtt. Each
recording is then replayed through the live receive path (`SdrReceiver`, the
pass/overlap demodulator, `recover`) and scored against what the *other*
receivers heard at the same moment -- the mesh log's fused events -- so a
gain is judged on the same messages, not on whatever the air happened to
carry. The dongle's own score on those messages is reported too: it is the
control for the air changing between runs.

    # the pod owns the dongle: stop it first, restart it after
    microk8s kubectl delete pod insteonrf-v3
    tools/sdr_gain_sweep.py record --out DIR --gains 37.2,12.5,28.0,...
    microk8s kubectl apply -f /k8s/yaml/insteonrf-v3.yaml
    tools/sdr_gain_sweep.py score DIR

Clipping cannot be undone afterwards, so gains must be separate recordings;
repeat one gain at the start and the end to see how much the air drifted.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TARGETS = (
    # weak (V4 median ACK SNR 15-17 dB) ... strong (~32 dB)
    "30.BD.0B", "29.4D.F8", "28.DB.15", "24.78.BE", "29.4E.52", "2B.A0.AB",
    "27.87.4F", "1C.AF.1A", "2C.85.7B", "40.CE.68", "38.18.68", "24.70.E3",
)
PLM = "2B.93.07"
RATE = 2_400_000
MESH_LOG = "/nvme/churn/insteonrf-mesh/log/insteon-rf-mesh.jsonl"


def _mqtt_client(host: str) -> object:
    import paho.mqtt.client as mqtt

    def secret(key: str) -> str:
        out = subprocess.run(["microk8s", "kubectl", "get", "secret", "mosquitto", "-o",
                              f"jsonpath={{.data.{key}}}"], capture_output=True, check=True)
        import base64
        return base64.b64decode(out.stdout).decode()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"gain-sweep-{os.getpid()}")
    c.username_pw_set(secret("username"), secret("password"))
    c.connect(host, 1883)
    c.loop_start()
    return c


def record(a: argparse.Namespace) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gains = [g.strip() for g in a.gains.split(",")]
    targets = a.targets.split(",") if a.targets else list(DEFAULT_TARGETS)
    client = _mqtt_client(a.mqtt)
    runs = []
    for i, gain in enumerate(gains):
        path = out / f"run{i:02d}-g{gain}.iq"
        argv = ["rtl_sdr", "-d", a.device, "-f", str(a.freq), "-s", str(RATE), "-g", gain,
                "-n", str(int(a.seconds * RATE)), str(path)]
        t_start = time.time()
        proc = subprocess.Popen(argv, stderr=subprocess.PIPE)
        time.sleep(3)
        if proc.poll() is not None:
            sys.exit(f"rtl_sdr exited: {proc.stderr.read().decode()[-400:]}")  # type: ignore[union-attr]
        sent = []
        for r in range(a.rounds):
            for addr in targets:
                session = f"sweep-{i}-{r}-{addr}"
                client.publish(f"insteon/command/{addr}",  # type: ignore[attr-defined]
                               json.dumps({"cmd": "get_engine", "session": session}))
                sent.append((time.time(), addr))
        proc.wait()
        t_end = time.time()
        runs.append({"gain": gain, "file": path.name, "t_start": t_start, "t_end": t_end,
                     "queries": sent})
        print(f"gain {gain}: {path.stat().st_size / 1e6:.0f} MB, {len(sent)} queries", flush=True)
        time.sleep(a.gap)
    (out / "manifest.json").write_text(json.dumps(
        {"device": a.device, "freq": a.freq, "targets": targets, "runs": runs}, indent=1))
    return 0


def _replay(path: Path, t0: float) -> list[dict]:
    """Decode a recording exactly as the pod would, stamped with file time + t0."""
    from insteonrf.radio import sdr
    from insteonrf.recover import recover_from_burst

    sdr.SampleClock.at = lambda self, s: t0 + s / self.rate  # type: ignore[method-assign]
    rx = sdr.SdrReceiver("rtl_sdr", demod="numpy", signed=False)
    out = []
    with open(path, "rb") as fh:
        rx._iq = fh
        for b in rx._iter_numpy():
            recs = recover_from_burst(b) if b.header_index is not None or b.bits else []
            if not recs and b.header_index is not None:
                out.append({"t": b.timestamp, "fail": True, "snr": b.snr_db, "clipped": b.clipped})
            for rec in recs:
                p = rec.packet
                if not p.crc_ok:
                    continue
                out.append({"t": b.timestamp, "from": str(p.from_addr), "to": str(p.to_addr),
                            "cmd1": p.cmd1, "ack": p.ack, "snr": b.snr_db, "cfo": b.cfo_hz,
                            "clipped": b.clipped, "corrected": rec.corrected})
    return out


def _key(r: dict) -> tuple:
    return (r["from"], r["to"], r["cmd1"], bool(r["ack"]))


def score(a: argparse.Namespace) -> int:
    d = Path(a.dir)
    man = json.loads((d / "manifest.json").read_text())
    targets = set(man["targets"])
    lo = min(r["t_start"] for r in man["runs"]) - 5
    hi = max(r["t_end"] for r in man["runs"]) + 5
    events = []
    with open(a.mesh_log) as fh:
        for line in fh:
            e = json.loads(line)
            if lo <= e.get("first_seen", 0) <= hi:
                events.append(e)
    rows = []
    for run in man["runs"]:
        # rtl_sdr starts streaming ~1.1 s after it is launched; calibrated below.
        t0 = run["t_start"] + 1.1
        pk = _replay(d / run["file"], t0)
        ok = [p for p in pk if not p.get("fail")]
        ev = [e for e in events if run["t_start"] <= e["first_seen"] <= run["t_end"]
              and e.get("cmd1") == 0x0D and {e["from"], e["to"]} & targets
              and PLM in (e["from"], e["to"])]
        # Clock calibration: median offset to matching events.
        offs = []
        for p in ok:
            cands = [e for e in ev if (e["from"], e["to"], e["cmd1"], bool(e["ack"])) == _key(p)]
            if cands:
                near = min(cands, key=lambda e: abs(p["t"] - e["first_seen"]))
                if abs(p["t"] - near["first_seen"]) < 2:
                    offs.append(p["t"] - near["first_seen"])
        off = st.median(offs) if offs else 0.0
        for p in pk:
            p["t"] -= off
        hit = dongle = 0
        weak_tot = weak_hit = 0
        acks_tot = acks_hit = 0
        for e in ev:
            k = (e["from"], e["to"], e["cmd1"], bool(e["ack"]))
            got = any(_key(p) == k and e["first_seen"] - 0.1 <= p["t"] <= e["last_seen"] + 0.3
                      for p in ok)
            hit += got
            dongle += "dongle" in e["heard_by"]
            if e["ack"]:
                acks_tot += 1
                acks_hit += got
                if e["from"] in man["targets"][:8]:
                    weak_tot += 1
                    weak_hit += got
        matched = sum(1 for p in ok if any(
            _key(p) == (e["from"], e["to"], e["cmd1"], bool(e["ack"]))
            and e["first_seen"] - 0.1 <= p["t"] <= e["last_seen"] + 0.3 for e in ev))
        rel = [p for p in ok if {p["from"], p["to"]} & targets and p["cmd1"] == 0x0D]
        rows.append({
            "gain": run["gain"], "events": len(ev), "v3": hit, "dongle": dongle,
            "acks": f"{acks_hit}/{acks_tot}", "weak_acks": f"{weak_hit}/{weak_tot}",
            "copies": len(rel), "repaired": sum(1 for p in rel if p["corrected"]),
            "fails": sum(1 for p in pk if p.get("fail")),
            "snr": round(st.median([p["snr"] for p in rel]), 1) if rel else None,
            "clip": round(st.median([p["clipped"] for p in rel]), 2) if rel else None,
            "offset_s": round(off, 3), "unmatched_copies": len(ok) - matched,
        })
        print(json.dumps(rows[-1]), flush=True)
    print()
    print(f"{'gain':>5} {'events':>6} {'v3%':>5} {'dongle%':>7} {'acks':>7} {'weak acks':>9} "
          f"{'copies':>6} {'rep':>4} {'fail':>4} {'snr':>5} {'clip':>5}")
    for r in rows:
        n = r["events"] or 1
        print(f"{r['gain']:>5} {r['events']:>6} {100 * r['v3'] / n:>5.0f} "
              f"{100 * r['dongle'] / n:>7.0f} {r['acks']:>7} {r['weak_acks']:>9} "
              f"{r['copies']:>6} {r['repaired']:>4} {r['fails']:>4} {r['snr']!s:>5} {r['clip']!s:>5}")
    (d / "score.json").write_text(json.dumps(rows, indent=1))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--out", required=True)
    r.add_argument("--device", default="INST915")
    r.add_argument("--freq", type=int, default=914_990_000)
    r.add_argument("--gains", default="37.2,12.5,19.7,28.0,33.8,42.1,49.6,37.2")
    r.add_argument("--seconds", type=float, default=80)
    r.add_argument("--rounds", type=int, default=3)
    r.add_argument("--gap", type=float, default=5, help="seconds between runs")
    r.add_argument("--targets", help="comma-separated addresses (default: 12, weak to strong)")
    r.add_argument("--mqtt", default="192.168.88.5")
    s = sub.add_parser("score")
    s.add_argument("dir")
    s.add_argument("--mesh-log", default=MESH_LOG)
    a = p.parse_args()
    return record(a) if a.cmd == "record" else score(a)


if __name__ == "__main__":
    sys.exit(main())
