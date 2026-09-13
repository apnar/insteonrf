# Tools for Insteon's RF protocol

Receive, decode, build and transmit **Insteon 915 MHz RF** packets with an
rfcat dongle (TI CC1111 — e.g. a Yard Stick One) or with any SDR that can
stream 8-bit I/Q samples (rtl-sdr, HackRF).

Protocol reference: [Doc/pkt_format.md](Doc/pkt_format.md).
How the packet CRC was reverse-engineered: [Doc/crc.txt](Doc/crc.txt)
([blog post](http://make-it-hack.blogspot.com/2015/08/reverse-engineering-crc.html)).
DEF CON 23 slides: [Doc/insteon_defcon23.pdf](Doc/insteon_defcon23.pdf).

## Install

Python 3.10+ and, for the radio, the [rfcat](https://github.com/atlas0fd00m/rfcat)
library plus a CC1111 running the rfcat firmware.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pip install "git+https://github.com/atlas0fd00m/rfcat"   # only needed for recv/send
make                                                     # fsk2_demod + rf_clip (SDR path)
pytest                                                   # 35 tests, no hardware needed
```

Everything is available as `insteon-rf <command>` and, for old habits, as the
original script names (`rf_reciv.py`, `rf_send.py`, `print_pkt.py`,
`send_comm.py`, `split_pkt.py`).

## Data format

Raw SDR files are signed or unsigned 8-bit I/Q. Every stage after
demodulation exchanges **ASCII strings of `1` and `0`**, one burst per line;
empty lines are ignored and lines starting with `#` are metadata. This
sidesteps bit/byte-order and alignment questions (modeled after Jef
Poskanzer's NetPBM formats). Packets are found inside a line by searching for
the start header, so leading garbage and either bit polarity are fine.

## Commands

| Command | Legacy name | Purpose |
|---|---|---|
| `insteon-rf recv` | `rf_reciv.py` | Receive with rfcat; prints bit strings (`-D` decodes inline, `-t` timestamps, `--carrier` captures on carrier detect instead of syncing on the start header) |
| `insteon-rf print` | `print_pkt.py` | Decode bit strings into packets (`-v` for addresses/command names, `-l FILE` to log) |
| `insteon-rf pkt` | `send_comm.py` | Build a packet as a bit string (`-n` to just describe it) |
| `insteon-rf send` | `rf_send.py` | Transmit bit strings with rfcat (`-L MS` listens for replies afterwards) |
| `insteon-rf dump` | `split_pkt.py` | Frame-by-frame breakdown for debugging a demodulator |
| `fsk2_demod` | | C FSK2 demodulator: I/Q in, bit strings out (`-U` signed input for HackRF) |
| `rf_clip` | | Splits an I/Q stream into one file per burst (squelch based) |
| `rtl_reciv.sh`, `hackrf_reciv.sh`, `hackrf_xmit.sh` | | SDR wrappers (the HackRF ones need a `hackrf_transfer` patched to use stdin/stdout) |

`fsk2_mod` (SDR transmit) needs liquid-dsp and is still marked BROKEN.

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
insteon-rf recv | insteon-rf print -v          # rfcat
insteon-rf recv -D -t                          # same, decoded inline with timestamps
./rtl_reciv.sh | ./fsk2_demod | insteon-rf print
./hackrf_reciv.sh | ./fsk2_demod -U | insteon-rf print
```

Transmit (the source address must be linked to the target for i2cs devices —
typically your PLM's address):

```
insteon-rf pkt -s 13.25.80 -d 16.3F.E5 13 00 | insteon-rf send          # Off
insteon-rf pkt -s 13.25.80 -d 16.3F.E5 11 BF | insteon-rf send -L 800   # On 75%, show the ACK
insteon-rf pkt -s 13.25.80 -g 1 -b 11 00     | insteon-rf send          # group 1 broadcast On
insteon-rf pkt -r 07 : E5 3F 16 : 80 25 13 : 11 BF | insteon-rf send   # raw wire bytes, CRC added
insteon-rf pkt -s 13.25.80 -d 16.3F.E5 -e 2F 00 00 00 0F FF 01          # extended (ALDB read)
```

Library use:

```python
from insteonrf import Packet, parse_bits
p = Packet.build("13.25.80", "16.3F.E5", cmd1=0x11, cmd2=0xFF)
bits = p.to_bits()                     # on-air bit string
for q in parse_bits(bits): print(q.describe())
```

## Troubleshooting

- `insteon-rf reset` issues a USB reset when the dongle stops answering
  (`Error in resetup(): USBTimeoutError`).
- The dongle's radio registers persist while it stays powered, so a config
  left behind by another rfcat program is overwritten on every run.
- No packets but bursts arrive: check the polarity is not the issue (the
  parser tries both) and run `insteon-rf dump` on a captured line to see
  where the Manchester decode breaks.

## Layout

    insteonrf/packet.py     Packet class, CRCs, framing, parse_bits()/to_bits()
    insteonrf/manchester.py Manchester coding helpers
    insteonrf/cmds.py       command number -> name tables
    insteonrf/radio.py      rfcat configuration, receive/transmit
    insteonrf/cli.py        the commands above
    Src/                    C demodulator / burst splitter for SDR input
    tests/                  pytest suite with real captures as fixtures
