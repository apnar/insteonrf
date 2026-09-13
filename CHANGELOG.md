# Changelog

All notable changes to this project. Versions follow
[semantic versioning](https://semver.org/) loosely: the bit-string pipeline
contract and the legacy script names are treated as public API.

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
