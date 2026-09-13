# Tools for Insteon's RF protocol

Receive, decode, build and transmit **Insteon 915 MHz RF** packets with an
rfcat dongle (TI CC1111 — e.g. a Yard Stick One) or with any SDR that can
stream 8-bit I/Q samples (rtl-sdr, HackRF).

Protocol reference: [Doc/pkt_format.md](Doc/pkt_format.md).
USB troubleshooting write-up: [Doc/usb-notes.md](Doc/usb-notes.md).
How the packet CRC was reverse-engineered: [Doc/crc.txt](Doc/crc.txt)
([blog post](http://make-it-hack.blogspot.com/2015/08/reverse-engineering-crc.html)).
DEF CON 23 slides: [Doc/insteon_defcon23.pdf](Doc/insteon_defcon23.pdf).

## Install

Python 3.10+ and, for the rfcat radio, the
[rfcat](https://github.com/atlas0fd00m/rfcat) library plus a CC1111 running the
rfcat firmware. numpy is the only hard dependency — it carries the software
modulator/demodulator, so the SDR path works without a compiler.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,mqtt]"
pip install "git+https://github.com/atlas0fd00m/rfcat"   # only for --backend rfcat
make                                                     # optional: the faster C demodulator
pytest                                                   # ~100 tests, no hardware needed
```

Everything is available as `insteon-rf <command>` and, for old habits, as the
original script names (`rf_reciv.py`, `rf_send.py`, `print_pkt.py`,
`send_comm.py`, `split_pkt.py`, `mod_pkt.py`).

## Architecture

Three layers, each usable on its own:

```
  radio backends            signal processing        protocol
  ───────────────           ─────────────────        ────────
  RfcatRadio   (TX/RX) ───┐                       ┌─ Packet / Address / Flags
  SdrReceiver  (RX)    ───┼── bit strings ────────┤   parse_bits() / to_bits()
  SdrTransmitter (TX)  ───┤   "0110100…"          └─ CRCs, Manchester framing
  FileRadio    (tests) ───┘        ▲
                                   │
                  dsp.modulate_fsk2 / demodulate_fsk2  (numpy)
                  Src/fsk2_demod                       (C, same output)
```

    insteonrf/packet.py     Address, Flags, MsgType, Packet, CRCs, parse_bits()/to_bits()
    insteonrf/cmds.py       Command enum + command number -> name tables
    insteonrf/manchester.py Manchester coding helpers
    insteonrf/dsp.py        numpy FSK2 modulator / demodulator, burst splitter
    insteonrf/radio/        rfcat, SDR and file backends behind one protocol
    insteonrf/monitor.py    JSON-lines logging, mesh-repeat dedupe, MQTT
    insteonrf/debug.py      frame-by-frame dump for demodulator debugging
    insteonrf/cli.py        the commands below
    Src/                    C demodulator / burst splitter (the fast path)
    deploy/insteonrf.yaml   k8s Pod that logs all house RF to MQTT
    tools/usb_stress.py     USB robustness harness (not shipped)
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
| `insteon-rf monitor` | | Long-running logger: JSON lines to a rotating file and/or MQTT, mesh repeats deduped |
| `insteon-rf modulate` | `mod_pkt.py` | Bit strings → raw I/Q for an SDR transmitter |
| `insteon-rf demod` | | Raw I/Q → bit strings (`--demod numpy|c`) |
| `insteon-rf clip` | | Split an I/Q stream into one file per burst |
| `insteon-rf dump` | `split_pkt.py` | Frame-by-frame breakdown for debugging a demodulator |
| `insteon-rf reset` | | Bounded USB reset of a wedged rfcat dongle |
| `fsk2_demod` | | C FSK2 demodulator: I/Q in, bit strings out (`-U` signed input for HackRF) |
| `rf_clip` | | Splits an I/Q stream into one file per burst (squelch based) |
| `rtl_reciv.sh`, `hackrf_reciv.sh`, `hackrf_xmit.sh` | | Thin wrappers over the backends above |

Backends: `--backend rfcat` (default, TX and RX), `rtlsdr` (RX), `hackrf`
(RX, and TX via the numpy modulator), `file` (replays bit-string files).

## Examples

Regression check with the sample capture in `Dat/`:

```
./fsk2_demod -U < Dat/41802513110D2711018C00.dat | insteon-rf print
41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C
```

The line is `flags : to : from : cmd1 cmd2 crc pad...` in wire order (addresses
low byte first). A lower-case `crc` means the CRC matched; `CRC` flags a mismatch.
`-v` adds a decoded line:

```
$ insteon-rf recv -D -v -t
14:17:10.512  0F : 52 4E 29 : 07 93 2B : 0D 00 F8 00 00 AA     crc F8
    Direct: 2B.93.07 -> 29.4E.52  Get Insteon Engine Version (0x0D 0x00)  hops 3/3
14:17:10.601  2B : 07 93 2B : 52 4E 29 : 0D 02 9E 00 00 AA     crc 9E
    ACK of Direct: 29.4E.52 -> 2B.93.07  Get Insteon Engine Version (0x0D 0x02)  hops 2/3
```

Receive:

```
insteon-rf recv | insteon-rf print -v           # rfcat
insteon-rf recv -D -t                           # same, decoded inline with timestamps
insteon-rf recv --backend rtlsdr | insteon-rf print
insteon-rf recv --backend hackrf --demod numpy -D
insteon-rf demod -U capture.iq | insteon-rf print    # from a recorded file
```

Transmit (the source address must be linked to the target for i2cs devices —
typically your PLM's address):

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
| `to`, `from` | addresses as `16.3F.E5` (`from` is `null` on group broadcasts) |
| `group` | all-link group number on group broadcasts, else `null` |
| `cmd1`, `cmd2`, `command` | command bytes and the looked-up name |
| `ext_data` | the 13 extended-data bytes, or `null` |
| `crc`, `crc_ok`, `ext_crc_ok` | received CRC and whether it verified |
| `complete` | the frame index counted down to 0 (not a truncated burst) |
| `raw` | every wire byte as hex — `Packet.from_dict()` rebuilds the packet from it |
| `repeats`, `hops_seen` | `monitor` only: how many mesh copies were folded in, and their hop counts |

`monitor` holds each record for its dedupe window (250 ms by default,
`--window`) before writing it, so the logged `repeats`/`hops_seen` cover every
copy of the message; `--no-dedupe` writes each repeat as it arrives instead.

## Troubleshooting

- **Dongle stops answering** (`Error in resetup(): USBTimeoutError` once a
  second): the backend now detects this and USB-resets and reopens the dongle
  by itself (`--no-auto-reset` turns that off). `insteon-rf reset` does it by
  hand and gives up after 10 s instead of hanging. If a reset does not help,
  unplug and replug.
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
- **Weak SDR reception**: the numpy demodulator integrates over each bit and
  copes with far more noise than the C one; try `--demod numpy`.
