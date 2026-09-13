# insteonrf — handoff plan: fix the two open issues and modernize the code base

Audience: an engineer or agent (Opus) picking this up cold. Everything below is
checked against the repo as of 2026-09-13, right after the Python 3 port
(see `CLAUDE.md` for the current layout and hardware facts). Work on this host
(`alpha`, the Yard Stick-class CC1111 "DONSDONGLE" is on USB `1d50:6048`), in
`/root/insteon/insteonrf`, venv `.venv`.

Do not commit or push without Josh asking. Keep every RF transmission benign
(Ping `0F`, Get Engine Version `0D`, Status Request `19`) and spoof only the
PLM address `2B.93.07`; the real PLM hears every ACK.

## 0. Current state (what you inherit)

- `insteonrf/` — `packet.py` (codec), `manchester.py`, `cmds.py`, `radio.py`
  (rfcat wrapper), `cli.py` (`recv send print pkt dump reset`). 35 pytest tests,
  fixtures in `tests/data/`. Legacy script names (`rf_reciv.py` …) are shims.
- `Src/` — C `fsk2_demod` (works, noisy warnings) and `rf_clip` (builds). No
  `fsk2_mod.c` exists in the tree at all: the Makefile target references a file
  that was never committed.
- Verified on hardware: receive/decode of live traffic; a spoofed Ping was ACKed
  by KPL `29.4E.52` (`insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf send -L 1500`).
- Test-traffic generator: publish `{"cmd":"get_engine","session":"x"}` to
  `insteon/command/<addr>` via the Home Assistant `mqtt.publish` service
  (`mosquitto_pub` is not installed on the host or in the pod).

## 1. Issue A — SDR transmit path (`fsk2_mod`) is missing

**Symptom.** README and Makefile list `fsk2_mod` as the FSK2 modulator for the
HackRF transmit pipeline; it is marked BROKEN and the source is absent. The only
working transmitter is the rfcat dongle.

**Decision: implement the modulator in Python/numpy, not C, and drop liquid-dsp.**
Rationale: the job is trivial (generate a constant-envelope 2FSK baseband at
2.4 Msps from a bit string), numpy does it in a few lines, and a Python
implementation can be unit-tested against `fsk2_demod` with no hardware.

Spec for `insteonrf/dsp.py::modulate_fsk2(bits, *, sample_rate=2_400_000, baud=9124, deviation=75_000, signed=True, amplitude=100) -> np.ndarray[int8]`:

- Input: an on-air bit string from `Packet.to_bits()` (already inverted and
  padded). Output: interleaved I/Q int8 (signed, HackRF) or uint8 offset-128
  (`signed=False`, rtl-sdr style files used by `fsk2_demod -u`).
- Samples per bit = `sample_rate / baud` (263.05 — keep it fractional; use a
  cumulative phase accumulator, not integer repetition, or the demod's bit
  timing drifts over a 480-bit packet).
- Bit `1` → +deviation, bit `0` → −deviation (check the polarity empirically:
  `fsk2_demod -U` on the generated samples must decode to the same packet, and
  `-i` must be unnecessary; if it decodes only inverted, flip the sign and note
  why in the docstring).
- Prepend ~2 ms of preamble tone alternating at the bit rate (the dongle's
  `AA AA AA AA 66 66` equivalent) and append ~1 ms so the demod's squelch opens
  before the header. Ramp amplitude over the first/last 50 µs to limit splatter.
- CLI: `insteon-rf modulate [-s RATE] [-b BAUD] [-U|-u] [-o FILE]` reading bit
  lines on stdin, writing raw I/Q to stdout or `-o`. Update `hackrf_xmit.sh` /
  `hackrf_tx.sh` to consume it (`hackrf_transfer -t file` works unpatched when a
  temp file is used; document that the stdin variant needs the patched binary).

**Tests (no hardware).**
- Round trip: `Packet.build(...)` → `to_bits()` → `modulate_fsk2()` → run
  `./fsk2_demod -U` as a subprocess → `parse_bits()` equals the original bytes
  (skip if the binary is not built; `make` in a session fixture).
- Same for an extended packet, for `signed=False` with `-u`, and at a second
  sample rate (e.g. 2.048 Msps with `-s`).
- Add a numpy AWGN case at moderate SNR to catch timing-recovery regressions.
- Add `numpy` to `[project.dependencies]`; keep rfcat optional.

**Hardware check (only if a HackRF is attached; none was seen on this host).**
Transmit a Ping at 914.95 MHz and watch for the ACK with the rfcat dongle
(`insteon-rf recv -D -t` in a second terminal).

## 2. Issue B — the rfcat dongle wedged once on USB

**Symptom.** One run of `rf_reciv.py -D -t --sync` printed
`Error in resetup():USBTimeoutError(110, 'Operation timed out')` once a second
for 45 s and received nothing. A `pyusb` `dev.reset()` (which itself timed out
after 30 s) brought it back; every later run, in both carrier and sync modes,
was clean. The firmware is `DONSDONGLE r5535` (Feb 2015), no CC-Bootloader,
so reflashing needs a GoodFET — treat firmware as a last resort.

**What that error means in rflib.** `resetup()` loops forever re-opening the
device and pinging it. It runs when the receive thread's `_recvEP5` hits an
unexpected `USBError`. So the dongle either stopped answering EP5 or the host
still held the interface from a previous process.

**Plan.**
1. Reproduce with a stress script (`tools/usb_stress.py`, not shipped):
   open/configure/receive 2 s/close × 200; the same with `SIGTERM` delivered at
   random points (mimics `timeout`); the same with two processes racing to open
   the device (mimics a leftover background receiver). Log which one wedges.
2. Likely culprits to test in order: (a) a process killed while the receive
   thread is mid-transfer leaves the interface claimed — check
   `Radio.close()` runs on SIGTERM/SIGINT (install handlers in `cli.py` that
   raise `KeyboardInterrupt`); (b) rflib's `RfCat()` constructor with a
   `_init_on_reconnect` re-writing the radio config while the dongle is still
   in RX — always `setModeIDLE()` first (already done in `_common`); (c) the
   2017 upstream firmware fix for EP5 transfers whose length +5 equals the
   endpoint packet size (64) — count how often `RFrecv` blocks are 59 bytes.
3. Make the tool self-healing regardless of root cause:
   - In `Radio.__init__`, if `RfCat()` does not answer `getBuildInfo()` within
     ~5 s, issue the pyusb reset, wait 3 s, retry once; surface a clear error
     after that.
   - Give `Radio.receive()` a watchdog: if N consecutive USB timeouts occur
     while `resetup` chatter is seen, close, reset, reopen, reconfigure. Expose
     as `--auto-reset` (default on) in `recv`/`send`.
   - Run rflib with `_quiet=True`/redirect its stderr so a user sees one line
     ("dongle not responding, resetting…") instead of the loop.
   - Make `insteon-rf reset` bounded: pass a timeout to `dev.reset()` where
     pyusb allows it, otherwise run it in a thread and give up after 10 s with
     a message to replug.
4. Document the outcome in `CLAUDE.md` ("Facts worth knowing") and README
   troubleshooting. Only if (2c) is implicated and the wedge recurs under the
   watchdog: build `RfCatDonsCCBootloader.hex` from a pinned SDCC and flash via
   GoodFET so future updates go over USB.

**Acceptance.** 200 open/receive/close cycles and 50 kill-mid-receive cycles
without a manual reset; `insteon-rf recv` survives an injected reset (unplug
is fine as a proxy) and resumes decoding.

## 3. Refactor into a modern, usable code base

Keep the ASCII bit-string pipeline contract and the legacy script names; both
are load-bearing for existing users and the README. Everything else below is
fair game. Suggested order; each step leaves the tests green.

### 3.1 Typed protocol model (`packet.py`)
- `Address` value type (`__slots__`, parses/prints `16.3F.E5`, `.wire` gives
  the 3 low-first bytes; hashable; `__eq__` with str). Replace the
  `parse_addr`/`addr_to_wire`/`wire_to_addr` trio.
- `MsgType(IntEnum)` for the 8 flag types, `Flags` dataclass with
  `from_byte()`/`to_byte()`; `Packet.msg_type` returns the enum.
- `Command(IntEnum)` for the well-known cmd1 values (`ON = 0x11` …); `cmds.py`
  keeps the descriptive tables but the enum is what code uses.
- `Packet` stays a dataclass over wire bytes, but add `__eq__` on `data`,
  `__bytes__`, `Packet.parse(bytes)` and `to_dict()`/`from_dict()` for JSON.
- Move `dump_frames` into `insteonrf/debug.py`.

### 3.2 Radio backends (`radio.py` → `radio/`)
- `RadioBackend` protocol: `configure_rx()`, `configure_tx()`,
  `receive_bits(timeout) -> (ts, bits) | None`, `transmit_bits(bits, repeat, gap)`,
  context manager. Implementations: `RfcatRadio` (current code + the watchdog
  from §2), `SdrReceiver` (spawns `rtl_sdr`/`hackrf_transfer`, pipes through
  `fsk2_demod`; replaces `rtl_reciv.sh`/`hackrf_reciv.sh`), `SdrTransmitter`
  (uses `dsp.modulate_fsk2` + `hackrf_transfer -t`; replaces `hackrf_*.sh`),
  and `FileRadio` for tests (replays `tests/data/*.txt`).
- Selection via `--backend rfcat|rtlsdr|hackrf|file` on `recv`/`send`; keep the
  shell scripts one release as thin wrappers, then delete.

### 3.3 Signal processing in Python (`dsp.py`)
- Besides the modulator (§1), port `fsk2_demod` to numpy: quadrature
  discriminator (`np.angle(x[1:] * np.conj(x[:-1]))`), squelch on magnitude,
  bit slicer with fractional timing and resync on the Manchester edges. Keep
  the C binary as the fast path (`--demod c|numpy`), and make the two agree on
  `Dat/*.dat` and on the modulator's output in tests. This gives a pure-Python
  install for people without a compiler and a place to improve the demod.
- `rf_clip` equivalent: `insteon-rf clip` splitting I/Q into per-burst files.

### 3.4 Logging and integration (Josh's stated goal: log all house RF)
- `insteon-rf print --json`: one JSON object per packet (timestamp, addresses
  as strings, flags decoded, cmd1/cmd2, ext data, crc_ok, hops, raw hex).
- `insteon-rf monitor`: long-running receiver that writes JSON lines to a
  rotating file and optionally publishes each packet to MQTT
  (`--mqtt host:port --topic insteon/rf`, `paho-mqtt` optional dependency).
  Dedupe the hop repeats (same bytes except hops-left within 250 ms) into one
  record with a `hops_seen` field so logs are readable.
- Optional deployment: a k8s Pod manifest under `/k8s/yaml/` following that
  repo's conventions (bare Pod, hostPath for `/dev/bus/usb`, privileged,
  config on `/nvme/k8s/insteonrf-config`), publishing to the house broker at
  `192.168.88.5:1883`. Coordinate topic naming with insteon-mqtt's `insteon/`
  prefix (use `insteon-rf/`), and never transmit from that pod.

### 3.5 Tooling and hygiene
- `pyproject.toml`: add `numpy`; optional extras `radio` (pyusb, pyserial),
  `mqtt` (paho-mqtt), `dev` (pytest, ruff, mypy); document the rfcat git URL.
  Add `[tool.ruff]` (line length 110, rules `E,F,I,UP,B`) and `[tool.mypy]`
  (strict for `insteonrf/`, ignore `rflib`).
- Fix the C warnings in `fsk2_demod.c` (unused vars, `%d` vs `size_t`, missing
  return) and build with `-Wall -Wextra -Werror` in CI.
- GitHub Actions: `make`, `ruff`, `mypy`, `pytest` on 3.10–3.14 (Ubuntu). No
  hardware in CI; hardware tests are marked `@pytest.mark.hardware` and skipped
  unless `INSTEONRF_HW=1`.
- `logging` instead of ad-hoc stderr prints; `-v` raises the level.
- Delete `Makefile.kali` remnants, `insteonrf.egg-info/` (gitignore it),
  `Doc/pkt_format.txt` if it duplicates the `.md`.
- README: keep the fixture line `41 : 80 25 13 : 11 0D 27 : 11 01 8C 00           crc 8C`
  (asserted by a test); add a short architecture section and the JSON schema.
- Version `2.1.0`, a `CHANGELOG.md` starting with the 2.0.0 port notes.

## 4. Verification protocol (run before declaring any phase done)

```bash
. .venv/bin/activate
make && make test                                   # C + 35+ tests
insteon-rf recv -D -t -v                            # in one terminal
# in another: trigger traffic through Home Assistant → mqtt.publish
#   topic insteon/command/29.4E.52  payload {"cmd":"get_engine","session":"x"}
# expect: 0D 00 queries from 2B.93.07 and 0D 02 ACKs, hop by hop
insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | insteon-rf send -v -L 1500
# expect: "23 : 07 93 2B : 52 4E 29 : 0F 00 .. crc .." (the ACK)
```

Hardware sessions must end with the dongle idle and released; if
`Error in resetup()` appears, `insteon-rf reset`, wait 5 s, and record what
preceded it in the §2 investigation notes.

## 5. Out of scope / explicitly not doing

- Firmware update of the dongle unless §2 step 4 is reached.
- Replacing the PLM with the dongle (would need ACK generation and timing
  guarantees the rfcat USB path cannot give).
- Any transmit other than the benign commands listed at the top.
