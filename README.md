# Tools for Insteon's RF protocol

Receive, decode, build and transmit **Insteon 915 MHz RF** packets with an
rfcat dongle (TI CC1111 — e.g. a Yard Stick One), with any SDR that can
stream 8-bit I/Q samples (rtl-sdr, HackRF), or with an ESP32 + SX1262 board
running the ESPHome component in `esphome/`.

Several of those can run at once: `insteon-rf mesh` fuses what they each
heard into one stream and compares it against what the PLM heard. Three
radios have been on the air together since 2026-09-21 — see
[the listener mesh](#more-ears-for-the-plm-the-listener-mesh).

Protocol reference: [Doc/pkt_format.md](Doc/pkt_format.md).
USB troubleshooting write-up: [Doc/usb-notes.md](Doc/usb-notes.md).
How the packet CRC was reverse-engineered: [Doc/crc.txt](Doc/crc.txt)
([blog post](http://make-it-hack.blogspot.com/2015/08/reverse-engineering-crc.html)).
DEF CON 23 slides: [Doc/insteon_defcon23.pdf](Doc/insteon_defcon23.pdf).

## What's changed since the upstream fork

This fork starts from [evilpete/insteonrf](https://github.com/evilpete/insteonrf)
at its 2016 state, which described itself as a proof of concept for encoding
and decoding Insteon RF — and worked as one. What has happened since turned it
into something you can leave running, and then into something several radios
run at once. [CHANGELOG.md](CHANGELOG.md) has the detail; the short version:

**2.0.0 — ported and repackaged.** Python 2 → 3, and the loose scripts became
an installable `insteonrf` package with a single `insteon-rf <command>` entry
point. The old script names (`rf_reciv.py`, `print_pkt.py`, `send_comm.py`,
`split_pkt.py`, `rf_send.py`) still work as thin wrappers, and the ASCII
bit-string pipeline contract is unchanged, so existing pipelines keep running.
Added a pytest suite built on real captures.

**2.1.0 — radios, logging, reliability.** SDR transmit finally exists:
`insteon-rf modulate` generates the 2-FSK baseband in numpy, replacing the
`fsk2_mod` that upstream's Makefile referenced but never shipped (and dropping
the liquid-dsp dependency with it). The rfcat, rtl-sdr, HackRF and file radios
now sit behind one backend interface (`--backend`). `insteon-rf monitor` logs
every packet as JSON lines and/or MQTT, folding the mesh's hop repeats into one
record. Typed protocol model (`Address`, `Flags`, `MsgType`, `Command`), ruff +
mypy-strict, and CI across Python 3.10–3.14.

**2.2.0 — the receiver got about 17 dB better.** Sync correlation against the
known preamble/start-header locks the frame grid instead of guessing bit phase;
the carrier frequency offset is estimated and removed (real devices sit tens of
kHz off); every symbol is detected by a matched filter against both FSK tones,
which also yields a confidence; and `recover.py` decodes the frame layer using
the redundancy the protocol already carries — Manchester pairs combined by
their difference, the known frame-index counters as a checksum, and a bounded
CRC-guided repair search. See [Sensitivity](#sensitivity) for the measurements.

**2.5.x — more than one receiver.** `insteon-rf mesh` fuses captures from
several radios into one event per transmission, compares each against what the
PLM reported, and can hand the modem what it missed. A five-patch series for
insteon-mqtt adds the two topics that makes possible. The SDR backends became
usable live: `receive_burst()` had been spawning a fresh `rtl_sdr` per call,
and demodulating inline between pipe reads silently dropped the tail of every
packet.

**2.6.0 — confidence travels.** An I/Q receiver's per-symbol confidence used
to stop at the host that demodulated it. It now rides in the capture payload,
so fusion votes with it and a single damaged copy from an SDR can be repaired
where two hard copies could not.

**2.7.x — three radios on the air, and the reliability to trust them.** The
rfcat dongle had been wedging mid-run and spending three quarters of the day
deaf; the cure is re-arming receive on a short timer, not the USB reset that
had been assumed (see [Troubleshooting](#troubleshooting)). The monitor's
per-block work moved off the receive thread, which is what caused it. The
RTL-SDR V4 was deployed as a third listener. Two measurement bugs and two
classes of phantom decode were fixed — below.

### Bugs fixed that change what you decode

- **Extended-packet frame index.** Upstream emitted 31…1; the wire actually
  carries 31 then 30…0. Extended packets were mis-framed.
- **`fsk2_demod` bit polarity.** The phase discriminator called
  `fxpt_atan2(i, q)` on a function that takes `(y, x)`, which negates the phase
  and complemented every decoded bit. Packets still decoded — the parser
  accepts either polarity — but the demodulator's output was the complement of
  what was on the air, so generated samples came out inverted.
- **Either polarity, any alignment.** `parse_bits()` finds packets anywhere in
  a burst in either polarity, rather than requiring a clean aligned capture.
- **The dongle "wedging" on USB.** rflib never releases the USB interface, so
  the next run hit `Resource busy` and then an endless `Error in resetup()`
  loop. Fixed, with a self-healing watchdog — see [Doc/usb-notes.md](Doc/usb-notes.md).
- **The dongle going deaf mid-run**, which is a different fault and was
  diagnosed wrongly at first. The receiver stops delivering while the chip
  still answers register reads and still reports `MARC_STATE_RX` with correct
  modem registers and a normal noise floor. Measured against a second radio it
  was deaf for three quarters of a day. **Re-arming receive recovers it; a USB
  reset does not** — only 8 of 128 resets were followed by a decode inside a
  minute, while the dongle was re-enumerated 200 times for nothing. The
  threshold is now seconds rather than half an hour, and everything but
  draining the radio moved off the receive thread, which is what starved it.
- **Phantom packets, twice.** A frame sequence whose index counters are
  impossible is rejected even when its 8-bit CRC matches. That was not enough:
  a capture holds more than one packet and the parser tries every offset in
  both polarities, so a false lock in the tail can produce counters *and* a
  CRC that pass by luck, roughly one candidate in 256 against ~19,000 real
  messages a day. One such family looked like a neighbour's device for eight
  days. `Packet.hops_ok` now rejects a flags byte no transmitter can emit —
  hops-left above max-hops — which costs none of 18,829 real messages and
  removes 18% of the phantoms; an address allowlist covers the rest before
  anything is injected.

### Removed

`fsk2_mod` (never existed — use `insteon-rf modulate`), `Makefile.kali`, the
WAV-header readers and assorted one-off analysis scripts, and
`Doc/pkt_format.txt` (superseded by the corrected `pkt_format.md`). In the
library, `parse_addr()`/`addr_to_wire()`/`wire_to_addr()` were replaced by
`Address`, which compares and hashes like the strings it replaces.

## Getting started

### 1. What you need

Python **3.10 or newer** (tested through 3.14). numpy is the only hard
dependency. Then, depending on what you want to do:

| Goal | Hardware | Extra software |
|---|---|---|
| Decode captures, build packets, run the tests | none | — |
| Receive and transmit live | rfcat dongle: a CC1111 running rfcat firmware (e.g. a Yard Stick One) | `rflib`, from git |
| Receive with an SDR | rtl-sdr dongle, or a HackRF | `rtl_sdr` or `hackrf_transfer` on `PATH` |
| Transmit with an SDR | HackRF | `hackrf_transfer` |
| Receive on a small board, anywhere in the house | Heltec LoRa 32 V3, or any ESP32 + SX1262 | ESPHome; see [`esphome/`](esphome/) |
| Run several at once and compare | any two of the above | an MQTT broker |

An **RTL-SDR Blog V4** needs that project's fork of librtlsdr, not Osmocom's:
the stock library does not know the V4's R828D front end and mistunes it. It
is also the only receiver here that produces per-symbol confidence, which is
what lets a single damaged copy be repaired.

Insteon RF is **915 MHz**, so this is US-band hardware. Nothing here needs a C
compiler: the numpy demodulator is the default and is the more sensitive one.

### 2. Install

```bash
git clone https://github.com/apnar/insteonrf && cd insteonrf
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[radio,mqtt,dev]"

# the rfcat library is not usable from PyPI, so install it from git
pip install "git+https://github.com/atlas0fd00m/rfcat"
```

Pick fewer extras if you want less: `pip install -e .` is the core (numpy
only), `radio` adds pyusb/pyserial for the rfcat dongle, `mqtt` adds paho-mqtt
for `monitor --mqtt`, and `dev` adds pytest/ruff/mypy. `make` builds the legacy
C demodulator, which is optional — it is faster but much less sensitive than
the numpy default.

### 3. Check it works, with no radio at all

```bash
pytest                                              # ~400 tests, no hardware
insteon-rf demod -U Dat/41802513110D2711018C00.dat | insteon-rf print
```

That last line demodulates the sample capture and must print exactly:

```
41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C
```

You can also replay a recorded bit-string capture as if it were live, which is
a good way to see the decoder work before trusting your antenna:

```bash
insteon-rf recv --backend file --replay tests/data/rfcat-get-engine.txt -D -v
```

### 4. USB permissions for the rfcat dongle

`rflib` talks to the dongle over raw USB, so it needs access to
`/dev/bus/usb`. Either run as root, or install a udev rule — rfcat ships one,
and this is the minimal equivalent for the common dongle ids:

```bash
sudo tee /etc/udev/rules.d/20-rfcat.rules >/dev/null <<'RULE'
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6047", MODE="0664", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6048", MODE="0664", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="605b", MODE="0664", GROUP="plugdev"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Add yourself to `plugdev` (`sudo usermod -aG plugdev $USER`) and re-login, then
replug the dongle. If your distribution has no `plugdev` group, use one you are
in, or drop the `GROUP=` clause and widen `MODE` to `0666`.

### 5. Receive

```bash
insteon-rf recv -D -t -v          # decode inline, timestamped, with detail
insteon-rf recv | insteon-rf print -v
insteon-rf recv --backend rtlsdr -D        # or --backend hackrf
```

Trigger some traffic (press a switch, or have your hub poll a device) and
packets should appear within a second or two, usually several copies of each as
the mesh repeats them. If nothing appears at all: check the dongle is running
rfcat firmware (`insteon-rf recv -v` prints the radio config), check you have a
915 MHz antenna connected, and try `--carrier`, which captures on energy alone
instead of waiting for a valid start header.

### 6. Transmit

Transmitting needs a source address the target device has in its link
database — in practice your PLM's address, which is what the devices already
trust. An unlinked address gets ignored by i2cs devices.

```bash
# Ping 29.4E.52 as if from PLM 2B.93.07, then listen 1.5 s for the ACK
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf send -v -L 1500
insteon-rf pkt -n -s 2B.93.07 -d 29.4E.52 0F 00   # -n: describe it, send nothing
```

Only transmit to devices you own, and prefer harmless commands — `0F` Ping,
`0D` Get Engine Version, `19` Status Request — when you are exploring. A
spoofed command's ACK also reaches the real PLM, so your hub will see the
traffic too.

### 7. Log everything

```bash
export INSTEONRF_MQTT_USER=... INSTEONRF_MQTT_PASS=...
insteon-rf monitor -o insteon-rf.jsonl --mqtt 192.168.1.10:1883 --topic insteon-rf
```

One JSON object per packet, mesh repeats folded together, to a rotating file
and/or MQTT. [deploy/insteonrf.yaml](deploy/insteonrf.yaml) runs exactly this
as a receive-only Kubernetes Pod.

Every command takes `-h`, and `insteon-rf` on its own lists them. All of it is
also available under the original script names (`rf_reciv.py`, `rf_send.py`,
`print_pkt.py`, `send_comm.py`, `split_pkt.py`, `mod_pkt.py`).

## Architecture

Three layers, each usable on its own:

```
  radio backends            signal processing            protocol
  ───────────────           ─────────────────            ────────
  RfcatRadio   (TX/RX) ───┐                           ┌─ Packet / Address / Flags
  SdrReceiver  (RX)    ───┼── bits, or Burst ─────────┤   parse_bits() / to_bits()
  SdrTransmitter (TX)  ───┤   (bits + confidence)     ├─ CRCs, Manchester framing
  FileRadio    (tests) ───┘            ▲              └─ recover: soft framing,
                                       │                 index check, CRC repair
              dsp: modulate_fsk2 / find_sync → CFO → ML detection
              Src/fsk2_demod  (C, legacy: same bits on clean signal)
```

Receive chain, in order: envelope squelch finds candidate bursts → the known
preamble/start-header pattern is correlated to lock the frame grid, polarity
and carrier offset → every symbol is detected by correlating against both FSK
tones, which yields a *confidence* as well as a bit → `recover` combines
Manchester pairs by their difference, checks the known frame-index counters,
and flips the least-confident bits looking for a CRC match.

    insteonrf/packet.py     Address, Flags, MsgType, Packet, CRCs, parse_bits()/to_bits()
    insteonrf/cmds.py       Command enum + command number -> name tables
    insteonrf/manchester.py Manchester coding helpers
    insteonrf/dsp.py        numpy modulator, sync correlation, CFO, ML detector, Burst
    insteonrf/recover.py    soft-decision framing and CRC-guided repair
    insteonrf/context.py    remembers queries so replies name from the right table
    insteonrf/allon.py      phantom all-on triggers, context capture, attribution
    insteonrf/radio/        rfcat, SDR, file and MQTT backends behind one protocol
    insteonrf/monitor.py    JSON-lines logging, mesh-repeat dedupe, MQTT
    insteonrf/debug.py      frame-by-frame dump for demodulator debugging
    insteonrf/_version.py   the one place the version number is written
    insteonrf/cli.py        the commands below

  the mesh layer — several receivers, and the modem to compare against

    insteonrf/fusion.py     folds every receiver's copies of one message into
                            one event; votes across them, with confidence
                            where a receiver measured it
    insteonrf/plm.py        RF packet <-> the 02 50 / 02 51 frames a PLM hands
                            its host; message_key() is the hop-insensitive
                            identity both paths share
    insteonrf/inject.py     what may be handed to insteon-mqtt: tiers, shadow
                            mode, allowlist, suppression, rate limits
    insteonrf/mesh.py       the service and the miss table

    Src/                    C demodulator / burst splitter (legacy fast path)
    esphome/                ESPHome component + node config for an SX1262 board
    deploy/insteonrf.yaml   k8s Pod: the rfcat dongle, logging and mesh capture
    deploy/insteonrf-v4.yaml    the same for an RTL-SDR Blog V4
    deploy/insteonrf-mesh.yaml  the fusing service, no radio of its own
    deploy/insteon-mqtt/    patch series giving insteon-mqtt its raw topics
    tools/usb_stress.py     USB robustness harness (not shipped)
    tools/dsp_bench.py      sensitivity / false-accept benchmarks (not shipped)
    tests/                  pytest suite with real captures as fixtures

## Data format

Raw SDR files are signed (`-U`, HackRF) or unsigned (`-u`, rtl-sdr) 8-bit I/Q.
Every stage after demodulation exchanges **ASCII strings of `1` and `0`**, one
burst per line; empty lines are ignored and lines starting with `#` are
metadata. This sidesteps bit/byte-order and alignment questions (modeled after
Jef Poskanzer's NetPBM formats). Packets are found inside a line by searching
for the start header, so leading garbage and either bit polarity are fine.

## Commands

| Command | Legacy name | Purpose |
|---|---|---|
| `insteon-rf recv` | `rf_reciv.py` | Receive and print bit strings (`-D` decodes inline, `-j` JSON, `-t` timestamps, `--backend`, `--carrier`) |
| `insteon-rf print` | `print_pkt.py` | Decode bit strings into packets (`-v` for addresses/command names, `-j` JSON, `-l FILE` to log) |
| `insteon-rf pkt` | `send_comm.py` | Build a packet as a bit string (`-n` to just describe it, `-j` for JSON) |
| `insteon-rf send` | `rf_send.py` | Transmit bit strings (`-L MS` listens for replies, `-n` dry run) |
| `insteon-rf allon` | | Hunt phantom "all on" events; see [below](#hunting-phantom-all-on-events) |
| `insteon-rf monitor` | | Long-running logger: JSON lines to a rotating file and/or MQTT, mesh repeats deduped (`--mesh-capture NAME` makes this receiver a mesh member) |
| `insteon-rf mesh` | | Fuse listener captures, compare against the PLM, optionally inject; see [above](#more-ears-for-the-plm-the-listener-mesh) |
| `insteon-rf modulate` | `mod_pkt.py` | Bit strings → raw I/Q for an SDR transmitter |
| `insteon-rf demod` | | Raw I/Q → bit strings, or packets with `-D` (`--demod numpy|c`, `--method ml|discriminator`) |
| `insteon-rf clip` | | Split an I/Q stream into one file per burst |
| `insteon-rf dump` | `split_pkt.py` | Frame-by-frame breakdown for debugging a demodulator |
| `insteon-rf reset` | | Bounded USB reset of a wedged rfcat dongle |
| `fsk2_demod` | | C FSK2 demodulator: I/Q in, bit strings out (`-U` signed input for HackRF) |
| `rf_clip` | | Splits an I/Q stream into one file per burst (squelch based) |
| `rtl_reciv.sh`, `hackrf_reciv.sh`, `hackrf_xmit.sh` | | Thin wrappers over the backends above |

Backends: `--backend rfcat` (default, TX and RX), `rtlsdr` (RX), `hackrf`
(RX, and TX via the numpy modulator), `file` (replays bit-string files),
`mqtt` (RX, captures from the ESPHome listener boards).

## Hunting phantom "all on" events

Early Insteon devices (i1/i2, roughly pre-2012) act on an ALL-Link broadcast
addressed to **group 0** — "every device". The command was dropped from later
firmware, but legacy devices still obey it, so one malformed group-0 message
turns a whole house on at once, often at 3am.

**Your hub cannot catch this, structurally.** A PLM only passes up group
broadcasts it holds an ALDB link for, so an unlinked group-0 broadcast is
filtered out before the host sees it. That is also why the lights show as *off*
in Home Assistant while they are physically on: responders to an all-link
command do not announce their new state, and the controller never saw the
command. Checked against a real installation — three such events, identified by
the owner firing the "Everything" scene off to recover — the PLM log contained
**no trigger at all** in the minutes beforehand, only the recovery.

An RF receiver has no ALDB filter, so it sees everything. Two properties make
it diagnostic:

- **RF packets carry a CRC.** A group-0 On arriving with a *valid* CRC was
  genuinely transmitted that way, so the corruption happened inside the sending
  device. A *failed* CRC means it was mangled in flight — a different problem.
- **Hop counts locate the origin.** A transmission leaves its sender with
  `hops_left == max_hops`, and each repeat decrements it. The copy with the hop
  counter intact is closest to the source, and its RSSI says how near.

```
# generate the list of groups your network legitimately uses
grep -oE '^  - modem: [0-9]+' /path/to/scenes.yaml | awk '{print $3}' | sort -nu > groups.txt

insteon-rf allon --known-groups groups.txt -v
```

Leave it running. It keeps a rolling 30-second buffer of *every* burst, and on
a trigger writes the whole window — the trigger, its repeats, and whatever the
mesh did next — plus an attribution report:

```
=== all-link-group-0 at 2026-07-08 03:29:58
    group 0 (ON) from 29.41.B5, hops 3/3, CRC valid — some device transmitted this
    CF : B5 41 29 : 00 00 00 : 11 00 23 00 00 AA     crc 23
    3 copies of this message in the 30s window:
      03:29:58 hops 3/3  -55.0 dBm  crc_ok=True <-- hop counter intact: closest to the source
      03:29:58 hops 2/3  -61.0 dBm  crc_ok=True
      03:29:58 hops 1/3  -67.0 dBm  crc_ok=True
    most suspicious senders so far:
      29.41.B5  score 100  group0=1 unknown_group=0 crc_fail=0 of 412 packets
```

Triggers are: a **group-0** broadcast (any command), an **On to a group your
network does not use** (needs `--known-groups`, since a novel group number is
only suspicious if you know which are real), and a **storm** of group
broadcasts. Device-side groups 1–8 are never suspicious — group 1 is a
switch's main load, 2–8 are KeypadLinc buttons, and battery sensors use 1–4
(a leak sensor sends 1 dry, 2 wet, 4 heartbeat), none of which appear in a
hub's scene list. A repeated trigger is reported once and then counted for
five minutes (`cooldown`), because an unattended watch that fills the disk
with the same fault buries the event you are waiting for. Every sender also accumulates a suspicion score from malformed
all-link traffic, CRC failures and repaired bits, so a repeat offender rises to
the top of the exit summary even between events.

[deploy/insteonrf.yaml](deploy/insteonrf.yaml) runs exactly this as a
receive-only Kubernetes Pod (built from [deploy/Containerfile](deploy/Containerfile)
— note it installs `libusb-1.0-0`, which `python:*-slim` lacks and without
which `rflib` cannot see the dongle). The pod exits with a one-line error if
the dongle is busy and lets the supervisor retry, which is what happens for a
moment when a previous process still holds the USB interface.

With `--mqtt --mqtt-alerts-only`, triggers publish to `<topic>/alert` (QoS 1)
and nothing else does — which is the right split for a long watch. Unsubscribed
MQTT messages do not pile up on a broker (they go out unretained at QoS 0 and
are simply dropped), but a broker running `log_type debug` writes a line per
publish, so a per-packet stream nobody reads costs log volume for nothing. The
rotating JSON-lines file is the record; MQTT is only there to wake something
up. And
because attribution leans on RSSI, **several receivers are much better than
one**: compare the RSSI of the hop-intact copy across nodes in different parts
of the house and the origin localises.

## More ears for the PLM: the listener mesh

A PLM hears only what reaches its single antenna, and passes up only the group
broadcasts it holds an ALDB link for. On this network that leaves a real gap:
**26 devices have no powerline path at all** — 3 mini-remotes, 21 water
sensors, 2 door sensors — and a battery device cannot be polled after the
fact. If its message did not reach the modem, it is gone. A missed leak alert
costs a floor.

So additional receivers decode what the modem missed and hand it over, which
makes insteon-mqtt effectively more sensitive without adding a second
transmitter. There is still exactly one transmitter and it is the PLM; none of
this can transmit, deliberately.

The full plan, the staged rollout and the findings are in
[`Doc/MESH-PLAN.md`](Doc/MESH-PLAN.md). In outline:

```
boards / dongle ──MQTT──> insteon-rf mesh ──MQTT──> insteon-mqtt
                               ^                         │
       insteon/raw/rx ─────────┘   (what the PLM heard)   │
                                                          v
                        miss table + insteon-rf-mesh.jsonl
```

### It starts with no new hardware, and then it grew

The rfcat dongle is a valid mesh receiver on its own, so the whole path — the
insteon-mqtt patch, the wire-format conversion, the injector and its refusals,
and the miss measurement that decides whether any of it is worth it — was
built and proven before any new hardware existed.

```bash
# Any receiver publishes raw captures as a mesh member
insteon-rf monitor --mqtt=192.168.88.5 --mesh-capture=dongle ...
insteon-rf monitor --mqtt=192.168.88.5 --mesh-capture=v4 --backend rtlsdr --gain 37.2 ...

# Fuse them and compare against what the modem heard
insteon-rf mesh --mqtt=192.168.88.5 --plm-addr=2B.93.07 \
  --known-addrs known.txt --report-every=3600
```

Three radios have run together since 2026-09-21. They are not redundant —
they fail differently, which is the entire point. Over one day against the
modem's own log (1,343 messages it reported, hop copies collapsed):

| | of what the modem heard | best at | notes |
|---|---|---|---|
| rfcat dongle | 74% | the replies devices send back (64% of group cleanup ACKs) | hard bits |
| Heltec / SX1262 | 37% | nothing in particular; steady and never absent | hard bits; ~⅓ of captures break a few bytes after sync |
| RTL-SDR V4 | 29% | cleanups and broadcasts (75% / 67%), and anything weak | the only soft decisions |
| any of the three | 82% | | |

The 18% none of them heard is dominated by one keypad, which is almost
certainly powerline-only traffic — something an RF listener cannot hear by
definition, rather than a receiver failing.

**Where it actually pays.** The 26 devices with no powerline path sent 389
messages in that day. The modem saw **three**. Searching its whole log for
four of those sensor addresses returns nothing at all.

| heard those 389 | count | heard by nobody else |
|---|---|---|
| RTL-SDR V4 | 321 | 265 |
| Heltec | 106 | 19 |
| rfcat dongle | 100 | 13 |
| the PLM | 3 | — |

The V4 recovers 83% of that traffic, three times either hard-bit receiver,
using the per-symbol confidence only it produces: 707 of the day's 758
repaired messages were rebuilt from a single V4 copy no other receiver could
vote on.

The miss table is what the measurement phase produces:

```
# miss table over 6.0h, 14 devices heard on RF
device          rf   plm  missed   rate   rssi  receivers
44.12.AB        23     4      19  82.6%    -97  dongle,v4
2B.A0.AB       104    98       6   5.8%   -102  dongle
```

If it turns out the modem misses almost nothing, that is a real answer and the
right move is to stop — you are left with a useful diagnostic topic and no
risk taken. Two things had to be fixed before those numbers could be trusted:
the modem's own transmissions were being counted as a device that misses
everything, and the "did the modem hear this too" verdict was taken before the
modem's copy could arrive, which was wrong about one mark in five.

### Handing messages back requires patching insteon-mqtt

`deploy/insteon-mqtt/` carries a five-patch series and a Containerfile adding
two topics, **both off by default**:

| Topic | Direction | Purpose |
|---|---|---|
| `insteon/raw/rx` | out | every inbound message, before the duplicate check — the ground truth for what the modem heard |
| `insteon/raw/inject` | in | messages to feed into the protocol stack |

Enable in `config.yaml` under `mqtt:`:

```yaml
  raw:
    enable_publish: True
    publish_topic: 'insteon/raw/rx'
    enable_inject: False        # leave off until the miss rate says otherwise
    inject_topic: 'insteon/raw/inject'
```

The base image is pinned **by digest**: the upstream tag is `:latest`, and an
unpinned pull would silently replace the patched image with a stock one,
turning injection off with no error. `pytest tests/test_inject.py` applies the
series to a pristine tree and exercises the result, so a version bump fails
there rather than on a running house.

### Injection is built to refuse

`insteon-rf mesh` injects nothing unless `--inject` names a tier, and even
then runs in shadow mode until `--live`. In order of how badly each would go
wrong:

- **Replies are never injected.** An ACK answers a command insteon-mqtt is
  actively waiting on, with its own handler state machine and timeouts.
- **The PLM's own transmissions are never injected.** The listeners hear the
  modem too, and its transmissions never come back as inbound messages, so
  they *look* like the misses of a device that misses everything.
- **Anything the modem already heard is suppressed** — with a fixed window,
  not upstream's. insteon-mqtt's duplicate window is `hops_left × 0.087`
  seconds, which is **zero** at no hops left; measured live, it processed two
  copies of one ACK 87 ms apart.
- **Addresses that are not on this network are never injected.** A decoder
  that occasionally invents a packet invents its sender too, and insteon-mqtt
  would create state for a device that does not exist. `--known-addrs FILE`
  names the real ones; eight days of capture produced 61 addresses that are
  not among them.
- **Unverified bytes are never injected.** CRC, the frame-index counters and
  the hop fields all have to hold, and a packet recovered by voting across
  receivers needs at least three of them to have voted.
- **Rate limits**, because every inbound message delays the modem's next
  transmit, so a flood would stall outbound commands.

Tiers are enabled one at a time: `battery` (the 26 unpollable devices — safest
and highest value), then `group`, then `state`.

### The ESPHome listener boards

`esphome/components/insteon_rf/` is an external component for the Heltec LoRa
32 V3 (ESP32-S3 + SX1262), receive only, no external library, building under
esp-idf. The SX1262 has no continuous-bitstream mode — the SX127x family
exposes DATA and DCLK pins for that and SX126x dropped it — so GFSK packet
mode is used as a raw bit recorder: sync word set to the invariant start of
every Insteon packet, preamble detector off, CRC off, whitening off, fixed
162-byte payload (sized to the 50 ms slot grid so no slot is cut in half). On-board, each capture must pass a **Manchester-validity
gate**: 26 of every 28 on-air bits are Manchester pairs, and a valid pair is
only `01` or `10`, so noise fails within a handful of bits. That is what makes
running with the preamble detector off viable in a crowded 915 MHz band.

**Status: running.** First flashed 2026-09-19 a few feet from the PLM, and it
received Insteon on the first probe — a CRC-valid Get Engine Version at
-92.5 dBm. Packet-mode sync with the preamble detector off works, and the
default polarity is right, which were the two assumptions the whole approach
rested on. It has been publishing to the mesh since.

Two things the first hour taught, both now handled: the radio *consumes* the
sync word, so captures arrive without their start header and the host has to
put it back; and `GetRssiInst` read after a capture measures whatever is on
air next, which a few feet from the modem is its own hop repeat — the packet's
own strength comes from `GetPacketStatus`.

Still open: roughly a third of its captures break their Manchester stream
120–170 bits after sync on packets the dongle decodes whole, and it is worst
on last-hop copies, where 85% fail. The leading hypothesis is bit-clock
tracking of transmitters a few tenths of a percent off nominal baud, which is
why the SDR path grid-searches symbol rate. `preamble_detector_bits: 8` is the
one-line A/B and has not been run. Fusion recovers most of those messages from
another receiver's copy, which is why this is a known cost rather than a
blocker.

The board reports its own firmware version as a sensor, matching
`insteonrf/_version.py`, because ESPHome's build timestamp only moves when the
config hash changes — a reflash of unchanged config is otherwise invisible.

## Command coverage

The command tables are checked against real traffic, not guesswork: 257,000
received messages from five months of PLM logs on a 136-device network (68
dimmers, 18 relay switches, 13 KeypadLincs, 11 FanLincs, 21 leak sensors, 3
remotes, 2 door sensors). **99.5% of those messages get a name.**

| Message class | Messages | Named |
|---|---|---|
| Group broadcast, standard | 236,962 | 100% |
| Direct, standard | 20,104 | 94.2% |
| Extended | 8,155 | 99.7% |

`tests/test_cmds.py` pins that result: every command the traffic contained is
asserted to have a name, so a table edit cannot silently regress it.

Device classes matter less than you'd think here. Leak sensors, door sensors
and remotes signal through **group numbers** rather than special commands — a
leak sensor sends plain `0x11`/`0x13` on group 1 (dry), 2 (wet) and 4
(heartbeat) — and KeypadLinc buttons and FanLinc speeds are likewise ordinary
commands distinguished by group and extended data. So there is no device class
whose vocabulary sits outside these tables.

### Replies are read in the query's table

An ACK is always a *standard* message even when it answers an extended command,
and it echoes the query's `cmd1`. Four numbers mean different things in the two
tables — `0x03`, `0x2E`, `0x2F`, `0x30` — so a reply cannot be named from one
packet alone. Measured on that same log, 3,712 of 3,722 standard `0x2F`
messages were ACKs of extended ALDB reads; naming them from the standard table
called every one of them "Light Off at Rate".

`insteonrf.context.CommandTracker` remembers recent queries, so `recv`,
`print`, `monitor` and `demod -D` name the reply from the query's table. Where
no query was seen, both meanings are reported ("Beep / Trigger ALL-Link
Command") rather than guessing:

```python
from insteonrf.context import CommandTracker
tracker = CommandTracker()
for pkt in stream:            # in arrival order
    tracker.observe(pkt)
    print(pkt.cmd_name)
```

### The remaining 0.47%

Unnamed messages are all low-numbered *direct* commands (`0x00`, `0x04`–`0x0E`),
and the evidence says they are corrupted reception rather than gaps:

- standard Insteon **powerline** messages carry no CRC at all — they rely on
  triple repetition, so mangled bytes reach the PLM and get logged;
- 53% of them arrive within 1.5 s of a *named* command from the same device;
- `hops_left=0`, the most-repeated copy, is over-represented (345 of 757);
- they cluster into 89 hours out of roughly 3,600.

Naming them would mean inventing protocol. **RF packets do carry a CRC**, so
they cannot reach this decoder at all — which is why insteonrf's view of your
network is cleaner than your PLM's.

If something genuinely new does turn up on the air, surface it rather than let
it hide in a log:

```
insteon-rf monitor --unknown-commands     # warns on each new one, summary at exit
grep '"command_known":false' insteon-rf.jsonl | jq -r .command | sort | uniq -c
```

## Sensitivity

Measured by `tools/dsp_bench.py`, adding noise to modulated packets and
counting exact recoveries (paired trials — every detector sees the same noise):

| symbol SNR | `fsk2_demod` (C) | numpy discriminator | ML | ML + soft frames | + repair |
|---|---|---|---|---|---|
| 31.6 dB | 0% | 100% | 100% | 100% | 100% |
| 25.6 dB | 0% | 72% | 95% | 98% | 98% |
| 23.7 dB | 0% | 0% | 100% | 100% | 100% |
| 13.2 dB | 0% | 0% | 99% | 100% | 100% |
| 11.6 dB | 0% | 0% | 90% | 100% | 100% |
| 10.7 dB | 0% | 0% | 48% | 95% | 96% |
| 9.7 dB | 0% | 0% | 22% | 94% | 94% |
| 8.5 dB | 0% | 0% | 0% | 66% | 74% |
| 7.2 dB | 0% | 0% | 0% | 22% | 30% |

Rows from 13.2 dB down were measured at 200 trials (±3% or better); the rest at
40 trials, where the columns are saturated and sampling error does not matter.
The C and discriminator columns were not re-run below 23.7 dB — both are 0%
there. `tools/dsp_bench.py` prints the binomial standard error with every
point, because near threshold a 40-trial sample is worth only about ±8%, which
is enough to make two honest runs look like they disagree.

50% recovery moves from about 25 dB (the previous numpy path) to about 11 dB
with matched-filter detection and about 8 dB once the frame layer decodes the
Manchester pairs and index counters instead of discarding them — **roughly
17 dB** end to end. The matched filter lands where theory says an uncoded
noncoherent 2-FSK detector should, and the ~3 dB past it is the Manchester
coding gain, which is free. No noise-only burst was ever accepted as a packet
(150 noise bursts, plus 2000 random bit strings), and a well-formed frame
sequence whose counters are impossible is rejected even when its CRC matches.

**On hard bits** — all a hardware demodulator like the CC1111 can give — the
repair path still helps, because a flipped symbol leaves an illegal Manchester
pair that marks where to look:

| symbol errors in the burst | `parse_bits` | with repair |
|---|---|---|
| 1 | 26% | 94% |
| 2 | 4% | 85% |
| 3 | 0% | 75% |
| 4 | 0% | 66% |

Run the benchmarks yourself:

```
python tools/dsp_bench.py                      # sensitivity sweep
python tools/dsp_bench.py --hard-bits          # dongle-style hard-bit repair
python tools/dsp_bench.py --false-accepts      # noise wrongly accepted
```

## Reading the output, and more examples

A decoded line is `flags : to : from : cmd1 cmd2 crc pad...` in wire order, so
addresses read low byte first — the sample capture decodes to

```
41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C
```

which is a Group Cleanup Direct from `27.0D.11` to `13.25.80`. A lower-case
`crc` means the CRC matched; `CRC` flags a mismatch. `-v` adds a decoded line:

```
$ insteon-rf recv -D -v -t
14:17:10.512  0F : 52 4E 29 : 07 93 2B : 0D 00 F8 00 00 AA     crc F8
    Direct: 2B.93.07 -> 29.4E.52  Get Insteon Engine Version (0x0D 0x00)  hops 3/3
14:17:10.601  2B : 07 93 2B : 52 4E 29 : 0D 02 9E 00 00 AA     crc 9E
    ACK of Direct: 29.4E.52 -> 2B.93.07  Get Insteon Engine Version (0x0D 0x02)  hops 2/3
```

Receiving, beyond the basics in [Getting started](#5-receive):

```
insteon-rf recv --carrier -D                    # capture on energy, not on a valid header
insteon-rf recv -D -j | jq .                    # JSON per packet
insteon-rf demod -U capture.iq -D -v            # a recorded I/Q file, with SNR and repairs
insteon-rf demod -U capture.iq -m discriminator # the old detector, for comparison
insteon-rf clip -U capture.iq -o burst          # split I/Q into one file per burst
insteon-rf dump < bits.txt                      # frame-by-frame, for demodulator debugging
```

Transmitting (the source address must be linked to the target for i2cs devices
— typically your PLM's address):

```
insteon-rf pkt -s 13.25.80 -d 16.3F.E5 13 00 | insteon-rf send          # Off
insteon-rf pkt -s 13.25.80 -d 16.3F.E5 11 BF | insteon-rf send -L 800   # On 75%, show the ACK
insteon-rf pkt -s 13.25.80 -g 1 -b 11 00     | insteon-rf send          # group 1 broadcast On
insteon-rf pkt -r 07 : E5 3F 16 : 80 25 13 : 11 BF | insteon-rf send   # raw wire bytes, CRC added
insteon-rf pkt -s 13.25.80 -d 16.3F.E5 -e 2F 00 00 00 0F FF 01          # extended (ALDB read)

# SDR transmit: modulate to a file, then key the HackRF
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf modulate -o ping.iq
hackrf_transfer -x 20 -a 1 -s 2400000 -f 914950000 -t ping.iq
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf send --backend hackrf
```

Log everything (this is what `deploy/insteonrf.yaml` runs):

```
export INSTEONRF_MQTT_USER=... INSTEONRF_MQTT_PASS=...   # keeps it out of ps
insteon-rf monitor -o /var/log/insteon-rf.jsonl --mqtt 192.168.88.5:1883 --topic insteon-rf
```

Library use:

```python
from insteonrf import Address, Command, Packet, parse_bits
from insteonrf.dsp import modulate_fsk2

p = Packet.build("13.25.80", "16.3F.E5", cmd1=Command.ON, cmd2=0xFF)
bits = p.to_bits()                      # on-air bit string
samples = modulate_fsk2(bits)           # int8 I/Q for an SDR
for q in parse_bits(bits):
    print(q.describe(), q.from_addr == Address("13.25.80"))
```

## JSON schema

`insteon-rf print -j`, `recv -D -j` and `monitor` emit one object per packet:

| Field | Meaning |
|---|---|
| `time`, `timestamp` | ISO-8601 local time and the raw epoch seconds (`null` if unknown) |
| `msg_type`, `msg_type_name` | e.g. `DIRECT_ACK` / `"ACK of Direct"` |
| `extended`, `ack`, `broadcast`, `group_msg` | flag bits |
| `hops_left`, `max_hops` | hop counters from the flags byte |
| `hops_ok` | false when hops-left exceeds max-hops, which no transmitter emits — the marker of a false header lock that passed the CRC by luck |
| `to`, `from` | addresses as `16.3F.E5` (`from` is `null` on group broadcasts) |
| `group` | all-link group number on group broadcasts, else `null` |
| `cmd1`, `cmd2`, `command` | command bytes and the looked-up name |
| `command_known` | false when the tables have no name for this command — see [Command coverage](#command-coverage) |
| `ext_data` | the 13 extended-data bytes, or `null` |
| `crc`, `crc_ok`, `ext_crc_ok` | received CRC and whether it verified |
| `corrected` | how many bits the repair search had to flip (0 for a clean decode) |
| `snr_db` | estimated burst SNR — soft demodulators only, `null` from the dongle |
| `rssi_dbm` | receiver signal strength when the radio reports it (see the caveat below) |
| `complete` | the frame index counted down to 0 (not a truncated burst) |
| `raw` | every wire byte as hex — `Packet.from_dict()` rebuilds the packet from it |
| `repeats`, `hops_seen` | `monitor` only: how many mesh copies were folded in, and their hop counts |

`monitor` holds each record for its dedupe window (250 ms by default,
`--window`) before writing it, so the logged `repeats`/`hops_seen` cover every
copy of the message; `--no-dedupe` writes each repeat as it arrives instead.

`insteon-rf mesh` writes the same object per *transmission* rather than per
copy, with the fields above plus:

| Field | Meaning |
|---|---|
| `heard_by` | one entry per receiver that heard it: `copies`, `crc_ok`, `rssi_dbm`, `snr_db`, `hops_left`. Each receiver's own figures, because they are not comparable between radios |
| `receiver_count`, `closest` | how many heard it, and which was nearest the source — most hops left, RSSI breaking the tie |
| `hops_left_min/max`, `hops_used` | across every copy from every receiver |
| `combined`, `combined_from` | true when no single copy verified and the packet was rebuilt by voting, and how many receivers voted |
| `plm_saw_it` | whether the modem reported the same message. Settled after a grace period, because the modem's copy travels the powerline, is parsed by insteon-mqtt and only then republished — it can arrive after the RF copy |
| `first_seen`, `last_seen` | the window the copies spanned |
| `repeat_index` | which retransmission of the message this event is |

## Troubleshooting

- **Dongle stops answering** (`Error in resetup(): USBTimeoutError` once a
  second): the backend detects this and USB-resets and reopens the dongle by
  itself (`--no-auto-reset` turns that off). `insteon-rf reset` does it by
  hand and gives up after 10 s instead of hanging. If a reset does not help,
  unplug and replug.
- **Dongle goes quiet while still answering** — a different fault, and the
  one that matters in a long run. Registers read fine, `MARC_STATE_RX` is set,
  the noise floor is normal, and not one block arrives. **Re-arm; do not
  reset.** `monitor --max-silence` is 20 s by default and re-arming is a few
  register writes that can only lose a packet already in flight. A USB reset
  was tried first and is nearly useless here: 8 of 128 were followed by a
  decode inside a minute. If your host has a second receiver, use it as the
  reference clock — silence is otherwise indistinguishable from a quiet house,
  and that ambiguity is why this went undiagnosed for days.
- **A capture decodes to a device you do not own.** Check the flags byte
  before believing it: hops-left above max-hops is impossible on the air, and
  it is the signature of a false header lock inside the tail of a real
  capture, whose frame counters and CRC can both pass by luck. `hops_ok` is in
  the JSON. If the flags look plausible too, compare the address against your
  own device list — and see whether the decode landed within a second of one
  of your own messages, which is what gives these away.
- **`USBError(16, 'Resource busy')`**: something still holds the interface.
  rflib's `cleanup()` does not release it — this package's `close()` does, and
  `recv`/`monitor`/`send` install SIGTERM handlers so a `timeout`/`kill` still
  runs it. A process killed with `SIGKILL` leaves it to the kernel; wait a
  second and retry.
- The dongle's radio registers persist while it stays powered, so a config
  left behind by another rfcat program is overwritten on every run.
- **No packets but bursts arrive**: polarity is not the issue (the parser tries
  both). Run `insteon-rf dump` on a captured line to see where the Manchester
  decode breaks.
- **Weak reception**: the numpy chain is far more sensitive than the C binary
  (see the table above), so prefer `--demod numpy`. `insteon-rf demod -D`
  decodes with soft decisions, which is better than piping bits into `print`
  because the pipeline's string contract discards confidence. Add `-v` to see
  per-burst SNR, carrier offset and how many bits were repaired.
- **`rssi_dbm` from the rfcat dongle is a snapshot, not a latched
  measurement.** It reads the CC1111's RSSI register just after a block
  arrives, while the radio is still in RX, so it reflects the channel a moment
  later rather than that packet exactly. It is good for ranking links and
  watching a device degrade over weeks — not for calibrated per-packet
  numbers, and not comparable with another receiver's. `--no-rssi` skips it.
  The SX1262 board reports the packet's own average, and the SDR path reports
  a symbol SNR instead; in a fused event each receiver's figure is kept
  separately under `heard_by`, because comparing receivers is the point of
  having more than one.
