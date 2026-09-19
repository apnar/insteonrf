# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tools for encoding/decoding the **Insteon 915 MHz RF protocol** with a TI CC1111 rfcat dongle
(the Yard Stick One attached to this host) or SDR hardware (rtl-sdr, HackRF). A Python 3
package (`insteonrf/`) plus a C FSK2 demodulator (`Src/`), glued together as Unix pipeline
stages that exchange ASCII bit strings. Ported from the original Python 2 proof of concept in
2026-09; the legacy script names are kept as thin wrappers. `CHANGELOG.md` has the history.

Protocol reference: `Doc/pkt_format.md` (packet layout, 28-bit-per-byte Manchester framing,
both CRC algorithms). `Doc/crc.txt` is the CRC reverse-engineering write-up.

## Build, test, run

```bash
. .venv/bin/activate        # venv with rfcat (rflib), pyusb, numpy, pytest, ruff, mypy, this package (-e)
pytest                      # ~390 tests, no hardware; real captures live in tests/data/
make                        # fsk2_demod + rf_clip (C, -Wall -Wextra -Werror); objects in Obj/
make check                  # ruff + mypy (strict) + pytest
insteon-rf                  # lists commands: recv send print pkt dump monitor modulate demod clip allon mesh reset

# Regression fixture (expected line is asserted by tests/test_packet.py):
./fsk2_demod -U < Dat/41802513110D2711018C00.dat | insteon-rf print
#   41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C

insteon-rf recv -D -t -v                                 # live decode from the dongle
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf send -L 1500   # Ping, show the ACK
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf modulate -o /tmp/ping.iq  # SDR TX
python tools/usb_stress.py all                           # USB robustness harness (hardware)
python tools/gen_sync_word.py                            # SX1262 sync word, from the TX path

# The listener mesh (Doc/MESH-PLAN.md). Phases 1-3 need no new hardware: the
# rfcat dongle is the first receiver.
insteon-rf monitor --mqtt=host --mesh-capture=dongle     # dongle joins the mesh
insteon-rf mesh --mqtt=host --report-every=900           # fuse + miss table, no injection

# Tests that use insteon-mqtt as an oracle, and verify the patch series:
kubectl exec homeassistant -c insteon -- tar cf - -C /opt/insteon-mqtt \
    insteon_mqtt config-example.yaml | tar xf - -C /tmp/imqtt
INSTEONRF_IMQTT=/tmp/imqtt pytest              # ~390 tests instead of ~350
```

Recreate the venv with `uv venv .venv && uv pip install -e ".[dev,mqtt]" pyusb pyserial
"git+https://github.com/atlas0fd00m/rfcat"` (a checkout is at `/root/insteon/rfcat-src`).

## Architecture

| Module | Role |
|---|---|
| `insteonrf/packet.py` | `Address`, `Flags`, `MsgType`, `Packet` (wire bytes + properties, `to_dict()`/`from_dict()`), `pkt_crc`/`ext_crc`, `parse_bits()` (find start header in either polarity, decode 28-bit frames), `Packet.to_bits()` (frame + Manchester + invert) |
| `insteonrf/manchester.py` | Manchester encode/decode, `invert_bits` |
| `insteonrf/cmds.py` | `Command` enum, cmd1/cmd2 → name tables, `lookup()` |
| `insteonrf/dsp.py` | numpy `modulate_fsk2()`; receive chain `find_sync()` → `estimate_cfo()` → `_ml_soft()` exposed as `demodulate_bursts()` (returns `Burst`: bits + per-symbol soft + sps + cfo + snr + header_index); `demodulate_fsk2()` is the string-contract wrapper; `iq_bursts()` splits bursts |
| `insteonrf/allon.py` | phantom all-on hunting: group-0/unknown-group/storm triggers, rolling context capture, hop-count+RSSI attribution, per-sender suspicion score (`insteon-rf allon`) |
| `insteonrf/context.py` | `CommandTracker`: correlates replies with their queries so an ACK's `cmd1` is read in the right table |
| `insteonrf/recover.py` | soft-decision framing: Manchester pairs combined by difference, known frame-index counters as a checksum, bounded CRC-guided bit repair (`recover_packets`, `recover_from_burst`) |
| `insteonrf/radio/` | `rfcat.py` (`RfcatRadio`, aliased `Radio`: 2FSK, 914.95 MHz, 9124 baud, 200 kHz BW, 75 kHz dev; `configure_rx()` syncs on the inverted start header `0x3155`, `sync_header=False` = carrier detect; `configure_tx()` sync word `0x6666`), `sdr.py` (`SdrReceiver`/`SdrTransmitter`), `file.py` (`FileRadio`), `__init__.py` (`RadioBackend` protocol, `open_backend()`) |
| `insteonrf/monitor.py` | JSON-lines writer with rotation, mesh-repeat `Deduper`, `MqttPublisher` |
| `insteonrf/debug.py` | `dump_frames()` — frame-by-frame breakdown |
| `insteonrf/cli.py` | `recv send print pkt dump monitor modulate demod clip reset`; `rf_reciv.py` etc. call these |
| `Src/fsk2_demod.c` | FSK2 demod + squelch + framing for raw 8-bit I/Q (`-U` signed/HackRF) |
| `Src/rf_clip.c` | split an I/Q stream into per-burst files |
| `insteonrf/plm.py` | RF packet <-> PLM `02 50`/`02 51` frames; `message_key()` is the hop-insensitive identity both paths share |
| `insteonrf/fusion.py` | multi-receiver fusion: hop/receiver folding, retransmission numbering, hops-left attribution, cross-receiver bit combining |
| `insteonrf/inject.py` | what may be handed to insteon-mqtt: tiers, shadow mode, PLM-heard suppression, rate limits |
| `insteonrf/mesh.py` | the mesh service and the miss table (`insteon-rf mesh`) |
| `insteonrf/radio/mqtt.py` | listener-board captures over MQTT, also a `RadioBackend` |
| `esphome/components/insteon_rf/` | ESPHome listener firmware (SX1262, receive only) |
| `deploy/insteonrf.yaml` | receive-only k8s Pod publishing to MQTT `insteon-rf/` (see `/k8s/yaml/AGENTS.md` conventions) |
| `deploy/insteonrf-mesh.yaml` | the mesh service as a second pod, no USB |
| `deploy/insteon-mqtt/` | patch series + Containerfile adding `insteon/raw/rx` and `insteon/raw/inject` to insteon-mqtt |

Pipeline contract: one burst per line of `0`/`1` characters; blank lines ignored; `#` lines are
metadata passed through. Packets inside a line may be in either polarity and at any offset.
The SDR stages (`modulate`, `demod`, `clip`) speak raw interleaved 8-bit I/Q instead.

## Facts worth knowing

- Wire order is `flags, to(3), from(3), cmd1, cmd2, [13 data, data-crc], crc, pad`; addresses
  are low byte first on the wire but shown as `16.3F.E5` by the decoder. For group broadcasts
  the sender sits in the "to" slot and `group 00 00` in the "from" slot.
- Frame index: first byte 31, then 11..0 (standard) or 30..0 (extended). The original code
  emitted 31..1 for extended packets; that was a bug.
- **Bit polarity**: a data `1` is the higher frequency (+75 kHz deviation) — the CC1111
  convention, confirmed by demodulating `Dat/41802513110D2711018C00.dat` with a standard
  discriminator and finding the inverted start header the dongle syncs on. `fsk2_demod` used
  to complement every bit because it called `fxpt_atan2(i, q)` on a `(y, x)` function; fixed
  2026-09-13, and `tests/data/sample-demod.txt` was regenerated. Packet-level output never
  changed, because `parse_bits()` accepts either polarity.
- i2cs devices only ACK senders in their link database — spoof the PLM (`2B.93.07` here).
  A spoofed command's ACK also reaches the real PLM, so keep test traffic benign (Ping `0F`,
  Get Engine Version `0D`, Status `19`).
- Radio registers persist in the dongle while powered; `RfcatRadio` re-writes all of them.
- **USB**: rflib's `cleanup()` does not release the USB interface or stop its three worker
  threads, so a second open (same process or next run) used to fail with
  `USBError(16, 'Resource busy')` and then spin in `Error in resetup()` forever. `close()`
  now idles the radio, clears `_threadGo`, releases and finalizes the interface — always
  use the context manager. rflib also *swallows* `KeyboardInterrupt` inside
  `USBDongle.recv()` (it prints a traceback and continues), so signals cannot stop a
  receive loop by exception alone: `cli.STOP` is an event set by the SIGINT/SIGTERM/SIGHUP
  handlers and checked after every block (`py-spy dump --pid` is how that one was found). On top of that, `resetup()` is bounded and quiet, opening is
  health-checked with one automatic USB reset, and `receive()` self-heals a dongle that only
  returns timeouts plus USB errors (`--no-auto-reset` to disable). `insteon-rf reset` is
  bounded to 10 s. After that fix, 200 open/receive/close cycles, 50 kill-mid-receive cycles
  and 20 two-process races run clean (`tools/usb_stress.py`); the dongle firmware
  (`DONSDONGLE r5535`, Feb 2015) was left alone.
- **Receive chain (2.2.0)**: envelope squelch → `find_sync()` correlates the known
  preamble+start-header against mean-removed discriminator output (CFO-immune) to lock the
  frame grid, polarity and offset → `estimate_cfo()` over the *balanced preamble only* (using
  the whole template biases it ~2.8 kHz, since the header has 14 ones of 27 bits) →
  noncoherent matched-filter detection of each symbol against both tones, repeated over a
  small symbol-rate grid, keeping the most confident result. A true sync scores 0.9-1.0; the
  best payload false alarm is ~0.7, so the threshold plus a one-packet minimum spacing and a
  relative floor keep payload from looking like a header.
- **Soft decisions are the point.** `Burst.soft` is signed per-symbol confidence, and
  `recover.py` uses it: Manchester is rate-1/2, so a logical bit is `sign(s1 - s0)` — worth
  ~3 dB over hard-deciding each symbol. The 5-bit frame-index counters are known from
  position (31, then 11..0 or 30..0) and are the guard that makes CRC-guided repair safe; an
  8-bit CRC alone would accept ~1 in 256 random attempts. `Packet.index_ok` carries the
  verdict, and `recover` refuses a packet whose counters are impossible even when its CRC
  matches (the CRC does not cover the index bits) — that is what killed a phantom
  `29.41.E0 -> 5D.99.3C cmd 0x69` decode seen on live air. Measured: 0 false accepts over
  150 noise bursts and 2000 random bit strings.
- **Numbers** (`tools/dsp_bench.py`, paired trials, 200 per point near threshold): 50%
  recovery at ~25 dB symbol SNR for the old discriminator path, ~11 dB for ML, ~8 dB with
  soft framing — ~17 dB gained. The matched filter sits where theory puts an uncoded
  noncoherent 2-FSK detector; the ~3 dB beyond it is Manchester coding gain. The C
  `fsk2_demod` fails even at 31 dB. Quote numbers from a 200-trial run: at 40 trials a
  near-threshold point carries ±8%, and an earlier table published 78%/85% at 8.5 dB where
  200 trials give 66%/74%. On *hard* bits (what the dongle gives), repair takes one-symbol-error
  recovery from 26% to 94%, two from 4% to 85%, three from 0% to 75%.
- `read_rssi()` reads the CC1111 RSSI register (offset 74 dB) *after* a block, while the
  radio is still in RX — a channel snapshot, not a latched per-packet measurement. Idle
  floor here is about -105 dBm; the loft KPL `2B.A0.AB` answers at about -102 dBm, i.e. it is
  a marginal link. `read_lqi()` is only meaningful right after a sync.
- **Command-name coverage is measured, not assumed.** `/k8s/insteon-config/log/insteon_mqtt.log`
  (~700 MB, 5 months) is the corpus: parse `Read 0x50: Std: <src>-><dst> N mh:X hl:Y cmd: c1 c2`
  for direct, the `grp:` variant for broadcast, and `Read 0x51: Ext:` for extended, then check
  `cmds.is_known()`. 99.5% of 257k messages are named; `tests/test_cmds.py` pins the observed
  distribution so a table edit cannot regress it.
- Broadcast `0x06` is the **ALL-Link Cleanup Status Report** and `cmd2` is the count of
  responders that did not answer — verified by correlating all 11,184 of them against
  insteon-mqtt's own "success"/"had N fails" log lines (it matched every time). It is the third
  most common message on this network after On and Off.
- The broadcast tables are the first step of a fallback chain (broadcast → standard → the
  unnamed label), not standalone lists; `BCAST_EXT_CMDS` is empty on purpose and falls through.
- **An ACK is a standard message even when it answers an extended command**, and it echoes the
  query's `cmd1` — so `0x03`, `0x2E`, `0x2F`, `0x30` (`cmds.ambiguous()`) cannot be named from
  one packet. Measured: 3,712 of 3,722 standard `0x2F` messages in the log arrived while an
  extended `0x2F` to that device was outstanding, i.e. ACKs of ALDB reads all named "Light Off
  at Rate" — 18% of direct traffic, confidently wrong. `context.CommandTracker` remembers
  queries (5 s window) and sets `Packet.ack_of_extended`; `cmd_name` then reads the right
  table, or reports both labels when no query was seen. Wired into recv/print/monitor/demod.
  Found live by firing a scene: an extended `0x30` ACK read "Beep".
- **In an ACK or NAK, `cmd2` is a reply, not a sub-command** (on-level, engine version, peeked
  byte), so `lookup(..., ack=True)` suppresses sub-table lookup. Without that, the ACK of Get
  Operating Flags read as "Set Operating Flags: LED On".
- The residual ~0.5% of unnamed traffic is **corrupted powerline reception**, not missing
  entries: standard powerline messages have no CRC, 53% of the unnamed arrive within 1.5 s of a
  named command from the same device, `hl:0` copies are over-represented, and they cluster into
  89 hours of ~3,600. Do not "fix" it by adding names. RF packets carry a CRC, so they never
  reach the decoder; use `monitor --unknown-commands` if something genuinely new appears.
- **Phantom all-on events (this house).** Legacy i1/i2 devices still obey an ALL-Link
  broadcast to group 0 = every device. A PLM only passes up group broadcasts it has an ALDB
  link for, so an unlinked group-0 broadcast never reaches the host — which is why HA (and
  the ISY before it) shows the lights as off while they are physically on, and why
  `/k8s/insteon-config/log/insteon_mqtt.log` contains **no trigger** for any event. Locate
  past events by the owner's recovery action instead: `grep '"group": "115"'` (the Everything
  scene off) found three in five months — 2026-05-29 14:47, **2026-07-08 03:30** (plus a
  Main Level Lights off at 01:43 the same night), 2026-07-21 22:05 — each preceded only by
  routine status polling. Power data cannot date them: events last seconds. Use
  `insteon-rf allon --known-groups <groups.txt>`; build the group list with
  `grep -oE '^  - modem: [0-9]+' /k8s/insteon-config/scenes.yaml | awk '{print $3}' | sort -nu`
  (114 legitimate groups, 0 not among them).
- **The dongle pod goes deaf** (2026-09-19: five gaps of 20–100 min in one day while the
  Heltec a few feet away kept capturing; also on about half of pod restarts, from the
  first second). Two flavours seen: still answering USB commands but delivering nothing,
  and — five minutes into a spell — answering nothing at all (firmware trace code
  `LCE_USB_EP5_TX_WHILE_INBUF_WRITTEN` afterwards). **A USB bus reset from a separate
  process while the pod holds the interface cures it within seconds (4 of 4);** the same
  reset from the pod's own process after releasing did not (0 of 4), so `heal()` spawns a
  child interpreter to reset *before* closing. `--max-silence=300`, counted from the last *decoded packet* (a deaf dongle still
  false-syncs on noise about once a minute and hands over junk blocks); `diagnostics()`
  (MARCSTATE, RSSI, firmware trace codes, SFRs, the modem registers) is logged around
  every watchdog action and on `SIGUSR1` — `kill -USR1 $(pgrep -f 'insteon-rf monitor')`
  from the host; read `kubectl logs insteonrf` for `radio on request` / `radio after N
  min` / `after heal`. To tell quiet air from a deaf dongle, compare against
  the Heltec's per-minute `Captures` sensor or `insteon-rf/rx/insteon-rf-main` on MQTT.
  From the host, `insteon-rf reset` is the operator's cure. **Never let a test or tool
  reset the dongle while the pod holds it**: `tests/test_radio.py`'s `fake_radio` stubs
  both `usb_reset` and `external_usb_reset` because, for one afternoon, every `make
  check` knocked the pod's receiver over and looked like a flaky dongle. `bpftrace -e
  'kprobe:usb_reset_device { printf("%s %d\n", comm, pid) }'` names the culprit.
- **SDR backends live**: `--demod numpy` (the C demodulator fails on real signals); the
  reader thread in `SdrReceiver._iter_numpy` is load-bearing — demodulating one packet
  takes ~85 ms and a pipe holds 14 ms of I/Q, so without it `rtl_sdr` drops samples
  silently. RTL-SDR Blog V4 needs the rtlsdrblog librtlsdr fork (installed in
  `/usr/local`) and `/etc/modprobe.d/blacklist-rtlsdr.conf`; gain 37.2 with squelch 12
  is right here (49.6 raises the floor to 6.8 and chatters).
- **Soft decisions travel in the capture payload** (2.6.0): `s` is one signed byte per
  bit of `b` (`±127` = clean), `snr` the symbol SNR. Only I/Q backends produce it;
  `Capture.soft`/`Sighting.soft` are `None` for the boards and the dongle. Fusion votes
  with confidence (hard copies vote `±1`) and tries the least-sure positions first when
  the CRC fails, so **one soft copy alone can be repaired** while hard copies still need
  two. `publish_capture` ships from just after the first header in either polarity and
  says which in `sw` — an SDR burst starts with preamble, unlike a FIFO dump.
- Generating test traffic on this host: publish to `insteon/command/<addr>` via the Home
  Assistant `mqtt.publish` service (no `mosquitto_pub` on the host or in the pod), e.g.
  payload `{"cmd":"get_engine","session":"x"}`; dual-band devices repeat it on RF.
- `Makefile.kali`, the WAV-header readers, `Doc/pkt_format.txt` and the never-committed
  `fsk2_mod.c` (liquid-dsp) are gone; `insteon-rf modulate` replaced the last of these.
- **Insteon RF repeating is synchronous simulcast on a slot grid.** Measured 2026-09-15
  over 14 captures: consecutive copies of a message start exactly **456 bits = 49.98 ms**
  apart (25 of 31 intervals; the rest are whole multiples), which is six half-cycles of
  60 Hz. The pitch does not vary with packet length, and no slot ever holds two
  overlapping copies — every repeater in earshot transmits the same hop simultaneously
  and the receiver decodes one clean packet. A packet occupies 364 of the 456 bits.
  Consequence: you cannot add a repeater without frequency-locking to the devices already
  in the slot (deviation is 75 kHz; a 20 kHz offset beats badly enough to wreck it). See
  `Doc/MESH-TRANSMIT.md`.
- `max_hops` is adaptive: insteon-mqtt logs `MsgHistory: Average hops N, using N` and
  lowers the budget for good links. Observed `mh:1`, `mh:2` and `mh:3` on different
  devices. Forcing 3 is the free first move for a device that misses commands.
- **Group broadcasts swap the two RF address slots**; they do not encode "group 00 00".
  Both slots are always a full three-byte address in wire order — a group broadcast leads
  with the sender, everything else leads with the destination. The destination's *low* byte
  is the group, and the upper bytes carry meaning: an ALL-Link Cleanup Status Report
  (`cmd1 0x06`) puts the reported command there, so `11.01.01` is "On, group 1". 27 of 223
  live captures were such reports.
- **insteon-mqtt runs from site-packages**, not `/opt/insteon-mqtt` (which is only the
  source checkout the hassio web CLI and docs live in). Patch both, and verify by importing
  `insteon_mqtt` with no `sys.path` manipulation — otherwise the image looks patched and
  behaves like stock. It also validates its config against a cerberus schema that rejects
  unknown keys under `mqtt:`, so a new config section needs a schema patch or the sidecar
  crash-loops and Insteon goes down.
- **insteon-mqtt's own duplicate window is `hops_left * 0.087` s** (`* 0.183` extended),
  which is *zero* at no hops left. Measured live: it processed two copies of one ACK, at
  hops 1 and 0, 87 ms apart. Anything feeding it messages must do its own suppression.
  Its `InpStandard.__eq__` ignores hops and max-hops — independently the same key as
  `monitor.Deduper` and `plm.message_key`.
- **The PLM's own transmissions are audible on RF** but never come back as inbound
  messages (they return as `0x62` echoes), so they look like the misses of a device that
  misses everything. Exclude the modem's address from any miss accounting, and never
  inject them.
- `Packet.bits` is truncated at the first damaged Manchester pair, so it is useless as a
  substrate for combining copies across receivers — vote over the whole capture
  (`fusion.sightings_from_capture`) instead.
- **SX126x GFSK `SetPacketParams` is nine bytes**, and the ninth is whitening. Sending
  eight leaves whitening undefined; enabled, it XORs every captured byte with PN9 and the
  Manchester gate rejects everything — a correctly wired board that looks dead. Register
  `0x06B8` is the whitening *seed*, not an enable. Found in the 2026-09-19 pre-flash review.
- A status byte is not a presence check for an SPI radio: with nothing on the bus MISO
  floats high and reads 0xFF. Write a register and read it back; that also catches CS on
  the wrong pin, the likeliest wiring mistake.
- Heltec LoRa 32 V3 pins, verified against Meshtastic `heltec_v3`: SCK 9, MISO 11, MOSI
  10, CS 8, RESET 12, BUSY 13, DIO1 14, TCXO 1.8 V on DIO3, DIO2 drives the RF switch,
  DC-DC. Board enumerates over USB as Espressif `303a:1001` (native USB-serial-JTAG);
  `303a:4001` on this host is the Nabu Casa Z-Wave stick, not a Heltec.
- Upstream insteon-mqtt's `Signal.connect` stores **weak references**: a lambda slot is
  collected as soon as `connect()` returns and the signal silently does nothing.
