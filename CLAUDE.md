# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tools for encoding/decoding the **Insteon 915 MHz RF protocol** with a TI CC1111 rfcat dongle
(the Yard Stick One attached to this host) or SDR hardware (rtl-sdr, HackRF). A small Python 3
package (`insteonrf/`) plus a C FSK2 demodulator (`Src/`), glued together as Unix pipeline
stages that exchange ASCII bit strings. Ported from the original Python 2 proof of concept in
2026-09; the legacy script names are kept as thin wrappers.

Protocol reference: `Doc/pkt_format.md` (packet layout, 28-bit-per-byte Manchester framing,
both CRC algorithms). `Doc/crc.txt` is the CRC reverse-engineering write-up.

## Build, test, run

```bash
. .venv/bin/activate        # venv with rfcat (rflib), pyusb, pytest and this package (-e)
pytest                      # 35 tests, no hardware; real captures live in tests/data/
make                        # fsk2_demod + rf_clip (C, SDR input path); objects in Obj/
insteon-rf                  # lists commands: recv send print pkt dump reset

# Regression fixture (expected line is asserted by tests/test_packet.py):
./fsk2_demod -U < Dat/41802513110D2711018C00.dat | insteon-rf print
#   41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C

insteon-rf recv -D -t -v                                 # live decode from the dongle
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf send -L 1500   # Ping, show the ACK
```

Recreate the venv with `uv venv .venv && uv pip install -e ".[test]" pyusb pyserial
"git+https://github.com/atlas0fd00m/rfcat"` (a checkout is at `/root/insteon/rfcat-src`).
`fsk2_mod` (SDR transmit) needs liquid-dsp and is still BROKEN.

## Architecture

| Module | Role |
|---|---|
| `insteonrf/packet.py` | `Packet` (wire bytes + properties), `pkt_crc`/`ext_crc`, `parse_bits()` (find start header in either polarity, decode 28-bit frames), `Packet.to_bits()` (frame + Manchester + invert), `dump_frames()` |
| `insteonrf/manchester.py` | Manchester encode/decode, `invert_bits` |
| `insteonrf/cmds.py` | cmd1/cmd2 → name tables, `lookup()` |
| `insteonrf/radio.py` | `Radio`: rfcat setup (2FSK, 914.95 MHz, 9124 baud, 200 kHz BW, 75 kHz dev), `configure_rx()` (default: sync on the inverted start header `0x3155`; `sync_header=False` = carrier detect), `configure_tx()` (sync word `0x6666` continues the Insteon preamble), `receive_bits()`, `transmit_bits()` |
| `insteonrf/cli.py` | `recv`, `send`, `print`, `pkt`, `dump`, `reset` subcommands; `rf_reciv.py` etc. call these |
| `Src/fsk2_demod.c` | FSK2 demod + squelch + framing for raw 8-bit I/Q (`-U` signed/HackRF) |
| `Src/rf_clip.c` | split an I/Q stream into per-burst files |

Pipeline contract: one burst per line of `0`/`1` characters; blank lines ignored; `#` lines are
metadata passed through. Packets inside a line may be in either polarity and at any offset.

## Facts worth knowing

- Wire order is `flags, to(3), from(3), cmd1, cmd2, [13 data, data-crc], crc, pad`; addresses
  are low byte first on the wire but shown as `16.3F.E5` by the decoder. For group broadcasts
  the sender sits in the "to" slot and `group 00 00` in the "from" slot.
- Frame index: first byte 31, then 11..0 (standard) or 30..0 (extended). The original code
  emitted 31..1 for extended packets; that was a bug.
- i2cs devices only ACK senders in their link database — spoof the PLM (`2B.93.07` here).
  A spoofed command's ACK also reaches the real PLM, so keep test traffic benign (Ping `0F`,
  Get Engine Version `0D`, Status `19`).
- Radio registers persist in the dongle while powered; `Radio` re-writes all of them. If USB
  calls time out (`Error in resetup()`), `insteon-rf reset` and wait a few seconds. Always
  close the `Radio` (context manager) — skipping `cleanup()` segfaults at interpreter exit.
- Generating test traffic on this host: publish to `insteon/command/<addr>` via the Home
  Assistant `mqtt.publish` service (no `mosquitto_pub` on the host or in the pod), e.g.
  payload `{"cmd":"get_engine","session":"x"}`; dual-band devices repeat it on RF.
- `Makefile.kali`, the WAV-header readers and other one-off analysis scripts from the original
  repo were removed in the port; see git history if needed.
