# Changelog

All notable changes to this project. Versions follow
[semantic versioning](https://semver.org/) loosely: the bit-string pipeline
contract and the legacy script names are treated as public API.

## 2.4.1 — 2026-09-13

Deployed the all-on watch as a pod and fixed what running it for real exposed.

- **Device-side groups are not suspicious.** The `unknown-group` trigger fired
  within a minute of going live, on a water sensor's group-4 heartbeat. Groups
  1-8 belong to devices, not to hub scenes — 1 is a switch's main load, 2-8 are
  KeypadLinc buttons, and battery sensors use 1-4 — so a known-groups list
  built from scene definitions never contains them. `DEVICE_GROUPS` now covers
  that; group 0 is still always flagged.
- **A cooldown on repeat triggers** (`cooldown_s`, 5 minutes, per kind/sender/
  group). An unattended watch that writes a dump per repetition of the same
  fault fills the disk and buries the event it was waiting for; repeats are now
  counted and reported in the summary instead.
- **`DongleError` exits with one line, not a traceback.** Under a supervisor
  (k8s `restartPolicy`, systemd) the retry is the supervisor's job. This
  happens routinely for a moment when a previous process still holds the USB
  interface.
- `deploy/Containerfile` plus a rewritten `deploy/insteonrf.yaml`: a locally
  built image (buildah → `ctr image import`) so restarts need no network, MQTT
  credentials from a secret, and the all-on watch enabled. The part that is
  easy to miss is `libusb-1.0-0`: `python:*-slim` does not ship it, and without
  it `rflib` cannot see the dongle at all.

## 2.4.0 — 2026-09-13

### Added

- **`insteon-rf allon` and `insteonrf/allon.py`** — hunting phantom "all on"
  events, where a malformed ALL-Link broadcast to group 0 turns a whole house
  on because legacy (pre-2012) devices still honour that command.

  The motivating insight is that a hub cannot see this: a PLM only passes up
  group broadcasts it holds an ALDB link for, so an unlinked group-0 broadcast
  never reaches the host — which is also why Home Assistant keeps showing the
  lights as *off* while they are on. Verified against a real installation:
  three events, located by finding the owner's "Everything"-scene-off recovery
  in five months of PLM logs (one at 03:30), and in every case the log held
  **no trigger** beforehand, only the recovery. An RF receiver has no such
  filter.

  `AllOnWatcher` keeps a rolling buffer of every burst and, on a trigger,
  writes the whole window plus an attribution report. It leans on two things RF
  gives you: a **CRC**, which separates "a device really transmitted this" from
  "the air mangled it", and **hop counts**, since a transmission leaves its
  sender with `hops_left == max_hops` so the hop-intact copy is closest to the
  source — with its RSSI as a distance hint, and several receivers as a
  triangulation method. Triggers: group-0 broadcast, On to a group the network
  does not use (`--known-groups`), and broadcast storms. Senders accumulate a
  suspicion score from malformed all-link traffic, CRC failures and repaired
  bits, so a repeat offender surfaces even between events.
- `monitor` gained `--watch-all-on`, `--dump-dir`, `--known-groups` and
  `--context-s`; with `--mqtt`, triggers publish as alerts.

## 2.3.0 — 2026-09-13

Command-name coverage, measured against a real network rather than assumed:
257,000 received messages in five months of PLM logs from a 136-device
installation (68 dimmers, 18 relay switches, 13 KeypadLincs, 11 FanLincs,
21 leak sensors, 3 remotes, 2 door sensors). Named coverage went from 95.3% of
broadcast and 94.2% of direct traffic to **99.5% overall**.

### Added

- **`0x06` ALL-Link Cleanup Status Report** in the broadcast table — 11,185
  messages in that log, the third most common thing on the air after On and
  Off, previously reported as "Bcast Command 0x06". Its `cmd2` is the number of
  responders that did not answer the cleanup, verified against all 11,184
  instances where insteon-mqtt's own success/"had N fails" verdict was logged
  alongside: `cmd2` matched the fail count every time.
- **`cmds.find()` and `cmds.is_known()`**, and a `command_known` field in the
  JSON, so unnamed commands can be found by query rather than by eye.
- **`insteon-rf monitor --unknown-commands`** warns the first time each unnamed
  command is seen (with sender and packet) and prints a summary at exit, so a
  device speaking something new surfaces instead of hiding in a log.

### Fixed

- **An ACK is no longer named from the wrong table.** An ACK or NAK is always a
  *standard* message even when it answers an extended command, and it echoes
  the query's `cmd1` — and `0x03`, `0x2E`, `0x2F` and `0x30` mean different
  things in the two tables. So every reply to an extended command was named as
  the standard command of the same number. This was not marginal: of the 3,722
  standard `0x2F` messages in that log, **3,712 arrived while an extended
  `0x2F` to that same device was outstanding** — ACKs of ALDB reads, all
  reported as "Light Off at Rate". That is 18% of direct traffic confidently
  mislabelled, which is worse than being unnamed.

  `insteonrf/context.py` fixes it the only way a receiver can, by remembering
  what was asked: feed `CommandTracker` the packets in arrival order and it
  marks each reply with whether its query was extended. `recv`, `print`,
  `monitor` and `demod -D` all do this now. With no context available, an
  ambiguous reply reports *both* meanings ("Beep / Trigger ALL-Link Command")
  rather than picking one.

  Found by firing a real scene and watching the air: an extended `0x30`
  Trigger ALL-Link Command came back as an ACK named "Beep".
- **The broadcast tables no longer dead-end.** They were thin standalone lists
  — 14 entries for standard, *empty* for extended — so a command with the same
  meaning in both contexts came out as "Bcast Command 0x09" despite being in
  the standard table. They are now the first step of a fallback chain
  (broadcast → standard), which is also why extended broadcasts decode at all.
- **`cmd2` is no longer read as a sub-command in an ACK or NAK.** There, `cmd2`
  is the device's reply — an on-level, an engine version, a peeked byte — so
  the old behaviour reported the ACK of Get Operating Flags as "Set Operating
  Flags: LED On". `lookup()` takes `ack=` and `Packet.cmd_name` passes it.
- Marked the inherited broadcast `0x04 → "Heartbeat"` entry as unverified: in
  five months that log has no broadcast `0x04` at all, and battery devices send
  their heartbeat as `0x11`/`0x13` on group 4.

### Not changed, deliberately

0.47% of that traffic is still unnamed, and the evidence says it is corrupted
reception rather than missing table entries: standard Insteon *powerline*
messages carry no CRC (they rely on triple repetition), 53% of the unnamed ones
arrive within 1.5 s of a named command from the same device, `hops_left=0` — the
most-repeated copy — is over-represented, and they cluster into 89 hours out of
some 3,600. Naming them would be inventing protocol. RF packets *do* carry a
CRC, so they cannot reach this decoder in the first place.

## 2.2.0 — 2026-09-13

Receiver rework: the detector now runs near the theoretical limit for uncoded
noncoherent 2-FSK, and the framing layer uses the redundancy Insteon already
carries. Measured with `tools/dsp_bench.py` (paired trials — every detector
sees the same noise), sweeping signal amplitude against a fixed noise floor:

| symbol SNR | `fsk2_demod` | numpy discriminator | ML | ML + soft frames | + repair |
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

The 50% point moves from ~25 dB to ~8 dB: **about 17 dB more sensitivity** than
2.1.0's numpy path, and the C binary needs more than 30 dB. False accepts
stayed at zero across 150 noise-only bursts and 2000 random bit strings.

### Added

- **Sync correlation** (`dsp.find_sync`). The known preamble + start-header
  pattern is cross-correlated against mean-removed discriminator output, which
  locates the frame grid, the polarity and the carrier offset instead of
  guessing bit phase from "the first zero crossing after the squelch opened".
  Candidates are filtered by absolute score, by a minimum spacing of one whole
  packet, and by a relative floor — payload contains plenty of preamble-like
  runs, and a true sync scores 0.9-1.0 where the best false alarm sits near 0.7.
- **Carrier frequency offset estimation and removal** (`dsp.estimate_cfo`),
  measured over the balanced preamble only. The capture in `Dat/` is ~24 kHz
  off (about 26 ppm at 915 MHz); uncorrected, that offset biases every symbol
  decision toward one tone.
- **Noncoherent matched-filter detection** (`method="ml"`, now the default):
  each symbol is correlated against both tones and the larger magnitude wins,
  which is the optimal detector for this waveform and yields a per-symbol
  *confidence*, not just a bit. `method="discriminator"` keeps the old chain.
- **Symbol-rate search**: a small grid around nominal, keeping the most
  confident result, so a few hundred ppm of transmitter clock error no longer
  walks the bit clock off over a 1000-symbol extended packet.
- **`insteonrf.recover`** — soft-decision framing. Manchester is a rate-1/2
  code, so a logical bit is decided from the *difference* of its pair (worth
  ~3 dB, and free); the 5-bit frame-index counters are known from position and
  become a checksum the protocol hands us; and the least-confident data bits
  are flipped in a bounded Chase search looking for a CRC match. Every repair
  must also satisfy the index check, which is what keeps an 8-bit CRC from
  accepting garbage.
- **Hard-bit repair too.** Given only hard bits — all the CC1111 can provide —
  illegal Manchester pairs mark the suspect positions. On the dongle's output:
  one symbol error 26% → 94% recovered, two 4% → 85%, three 0% → 75%.
  `recv`, `print`, `send -L` and `monitor` do this by default (`--no-repair`).
- **The frame-index invariant is enforced.** `Packet.index_ok` records whether
  the counters ran as the protocol requires, and `recover` refuses a packet
  whose counters are impossible however well its CRC matched — the CRC covers
  the data bytes only, so it cannot see a broken counter. `recv`/`print`/
  `monitor` drop those unless `--all` is given, which removes a class of
  phantom decode seen on live air.
- **Signal strength in the log**: `RfcatRadio.read_rssi()`/`read_lqi()` and
  `snr_db`/`rssi_dbm`/`corrected` fields in the JSON, so a day of `monitor`
  output shows which links are marginal. (A first run here found a loft
  KeypadLinc answering at -102.5 dBm against a -105 dBm noise floor.)
- `dsp.Burst` carries bits, per-symbol soft values, sample offset, symbol rate,
  CFO, SNR and the located header index; `SdrReceiver.iter_bursts()` and
  `receive_burst()` hand them to the CLI, so the live SDR path keeps its soft
  information instead of flattening to a string. `insteon-rf demod -D` decodes
  with soft decisions; `--method ml|discriminator` selects the detector.
- `tools/dsp_bench.py`: sensitivity sweeps, false-accept measurement and
  hard-bit repair measurement, so changes here are justified by numbers.

### Changed

- `demodulate_fsk2()` keeps its signature and string contract but now runs the
  ML chain; it is a thin wrapper over `demodulate_bursts()`, which is what you
  want if you care about sensitivity.
- `Packet` gained `corrected`, `snr_db` and `rssi_dbm` (all excluded from
  equality), and `to_dict()` reports them.

## 2.1.0 — 2026-09-13

### Added

- **SDR transmit path in Python.** `insteonrf.dsp.modulate_fsk2()` generates
  constant-envelope 2-FSK baseband I/Q (signed for HackRF, unsigned offset-128
  for rtl-sdr-style files) from an on-air bit string, with a fractional bit
  clock, a preamble/trailer tone and raised-cosine envelope ramps. Exposed as
  `insteon-rf modulate`. This replaces the never-committed `fsk2_mod.c` and
  drops the liquid-dsp dependency; the Makefile target is gone.
- **Software demodulator.** `insteonrf.dsp.demodulate_fsk2()` — quadrature
  discriminator, envelope squelch, fractional-timing bit slicer that integrates
  over the middle 60% of each bit. It agrees bit-for-bit with the C
  `fsk2_demod` on `Dat/*.dat` and on modulator output, needs no compiler, and
  tolerates roughly seven times more noise. `insteon-rf demod --demod c|numpy`
  selects either; `insteon-rf clip` is the `rf_clip` equivalent.
- **Radio backends** behind one protocol (`insteonrf.radio`): `RfcatRadio`
  (TX/RX), `SdrReceiver` (spawns `rtl_sdr`/`hackrf_transfer` and pipes through a
  demodulator), `SdrTransmitter` (modulator + `hackrf_transfer -t`) and
  `FileRadio` for tests. `--backend rfcat|rtlsdr|hackrf|file` on `recv`/`send`.
  The shell scripts are now thin wrappers over these.
- **`insteon-rf monitor`**: long-running logger writing one JSON object per
  packet to a rotating file and/or MQTT (`--mqtt host:port --topic insteon-rf`,
  needs the `mqtt` extra). Mesh repeats of one message — which differ in
  hops-left and therefore in the CRC — are folded into a single record with
  `repeats` and `hops_seen`. `deploy/insteonrf.yaml` runs it as a receive-only
  k8s Pod.
- **JSON output** for `print`, `recv -D` and `pkt` (`-j`), with
  `Packet.to_dict()` / `from_dict()` and the schema documented in the README.
- **Typed protocol model**: `Address` (parses/prints `16.3F.E5`, `.wire`,
  hashes and compares like its string form), `Flags` with
  `from_byte()`/`to_byte()`, `MsgType(IntEnum)`, `Command(IntEnum)`,
  `Packet.__bytes__`/`parse()`/equality on wire bytes.
- `tools/usb_stress.py`: open/receive/close, kill-mid-receive and two-process
  race harnesses for the USB path.
- ruff + mypy (strict) configuration, a GitHub Actions workflow (build, lint,
  types, tests on Python 3.10–3.14), a `hardware` pytest marker that is skipped
  unless `INSTEONRF_HW=1`, and `logging` in place of ad-hoc stderr prints.

### Fixed

- **`fsk2_demod` bit polarity.** The phase discriminator called
  `fxpt_atan2(i, q)` where the function takes `(y, x)`, which negates the phase
  and complemented every decoded bit. Packets still decoded (the parser accepts
  either polarity), but the demodulator's output was the complement of what was
  on the air, which would have made generated samples come out inverted. Both
  call sites now pass `(q, i)`; `tests/data/sample-demod.txt` was regenerated.
  The asserted README fixture line is unchanged.
- **The dongle "wedging" on USB.** `USBDongle.cleanup()` only resets rflib's
  queues: it leaves the three worker threads running and the USB interface
  claimed, so the next `RfCat()` — in the same process or the next run — hit
  `USBError(16, 'Resource busy')` and then the `Error in resetup()` retry loop.
  `close()` now idles the radio, stops those threads and releases and finalizes
  the interface. 200 open/receive/close cycles, 50 kill-mid-receive cycles and
  20 two-process races now run clean (`Doc/usb-notes.md`).
- **Ctrl-C and `kill` on a receiver.** `rflib`'s `USBDongle.recv()` catches
  `KeyboardInterrupt`, prints a traceback and carries on, so an interrupt never
  reached the receive loop: `insteon-rf recv` kept running (holding the USB
  interface) after a `kill`, and Ctrl-C printed an rflib traceback. The CLI now
  stops its loops through a `STOP` event set by the SIGINT/SIGTERM/SIGHUP
  handlers, and suppresses that traceback.
- `resetup()` is bounded and quiet instead of retrying forever once a second,
  opening the dongle is health-checked (`getBuildInfo` within 5 s) with one
  automatic USB reset and retry, `receive()` has a watchdog that resets and
  reopens a dongle that only produces timeouts plus USB errors
  (`--no-auto-reset` disables it), and `insteon-rf reset` gives up after 10 s
  with advice to replug rather than hanging in `pyusb`'s `reset()`.
- C warnings: unused variables, a `%d` for a `size_t`, and a missing return in
  `resync_shift()`. `Src/` now builds with `-Wall -Wextra -Werror`.

### Changed

- `numpy` is a hard dependency; extras are `radio` (pyusb, pyserial), `mqtt`
  (paho-mqtt), `dev` (pytest, ruff, mypy).
- `insteonrf/radio.py` became the `insteonrf/radio/` package (`Radio` is still
  exported as an alias of `RfcatRadio`), and `dump_frames()` moved to
  `insteonrf.debug`.
- `parse_addr()`, `addr_to_wire()` and `wire_to_addr()` were replaced by
  `Address`; `Packet.to_addr`/`from_addr` return `Address` objects, which
  compare and hash like the strings they replaced.
- `Packet.msg_type` returns a `MsgType` instead of an int (`msg_type_name` and
  `flags_byte` give the old values).
- Removed `Doc/pkt_format.txt` (superseded by the corrected `.md`) and the
  stale `fsk2_mod` build rules.

## 2.0.0 — 2026-09-13

Port of the original Python 2 proof of concept to Python 3 and to an
installable package.

- `insteonrf/` package: `packet.py` (codec, CRCs, framing), `manchester.py`,
  `cmds.py`, `radio.py` (rfcat wrapper), `cli.py` with the `recv send print pkt
  dump reset` subcommands, all installed as one `insteon-rf` entry point; the
  legacy script names became thin wrappers.
- Fixed the extended-packet frame index (the original emitted 31..1 instead of
  31 then 30..0), and made `parse_bits()` accept either bit polarity at any
  offset within a burst.
- pytest suite with real captures as fixtures; `Makefile.kali`, the WAV-header
  readers and assorted one-off analysis scripts were dropped.
