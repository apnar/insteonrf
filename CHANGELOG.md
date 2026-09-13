# Changelog

All notable changes to this project. Versions follow
[semantic versioning](https://semver.org/) loosely: the bit-string pipeline
contract and the legacy script names are treated as public API.

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
| 15.7 dB | 0% | 0% | 100% | 100% | 100% |
| 11.6 dB | 0% | 0% | 88% | 100% | 100% |
| 10.7 dB | 0% | 0% | 38% | 95% | 98% |
| 8.5 dB | 0% | 0% | 0% | 78% | 85% |

The 50% point moves from ~26 dB to ~7.5 dB: **about 18 dB more sensitivity**
than 2.1.0's numpy path, and the C binary needs more than 30 dB. False accepts
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
