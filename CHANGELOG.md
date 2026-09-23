# Changelog

All notable changes to this project. Versions follow
[semantic versioning](https://semver.org/) loosely: the bit-string pipeline
contract and the legacy script names are treated as public API.

## 2.8.0 — 2026-09-23

Four receivers in one house disagreed about what was on the air far more
than four radios should. Mostly they were not disagreeing: the mesh was
miscounting them, and one of them could not keep up.

- **Every receiver stamps the on-air time of its capture's first bit.** The
  Heltec sent `time(nullptr) * 1000` -- whole seconds, 0-1 s early at random
  (measured: 0.34-1.04 s before arrival); the dongle sent the USB arrival of
  its 255-byte block, 224 ms after the sync; the V4 sent the moment its
  buffer was handed to a backlogged demodulator (0.05-2.1 s late). Fusion
  groups by time, so one transmission became up to three events: of 3,858
  in a day, 367 were credited to all three receivers, 524 pairs of "different"
  events were the same message under two seconds apart, and each receiver
  looked as if it heard what the others missed (45% / 30% / 59%). Re-grouped
  with the offsets removed the same day reads dongle 75%, Heltec 41%, V4 42%.
  Now the board stamps at millisecond resolution less the time since RxDone
  and the capture's own air time, the dongle subtracts its block's air time,
  the V4 stamps each burst from its sample position (`SampleClock`), and
  packets later in a capture get `pos / 9124` added.
- **Fusion keeps two clocks apart.** Windows are on-air time; a bucket closes
  `--lateness` (1.0 s) later by the wall clock, so a receiver that delivers
  late still joins. A copy later than that is counted in `late_copies` and
  dropped -- before, it opened a new bucket and came out as a
  *retransmission*. The same bytes past a bucket's window start a new event
  even while the old one is still open. `ClockSkew` learns each receiver's
  constant offset from messages they share (hop copies normalised to the
  first slot) and corrects before matching (`--no-clock-skew` to disable).
- **The mesh reports what it needs to be checked.** The miss report ends with
  *receiver coverage*: every receiver scored against the same set of device
  messages, with how many it alone heard. The stats carry `late_copies`,
  per-receiver `arrival_lag` and `clock_offsets`.
- **The SDR demodulator keeps up.** It waited for the squelch to close, which
  back-to-back 50 ms slots postpone for a whole exchange; then each of 11
  symbol-rate hypotheses re-mixed the whole squelch run from each sync, and
  `find_sync` stopped at four. 2.1 s of busy air took 37 s and yielded 12 of
  40 packets; live, the pod dropped over a thousand I/Q chunks in 81 minutes.
  Now it streams in overlapping passes (0.2 s new + 0.12 s carried, longer
  than any packet), mixes once per sync and tries every rate on the sums,
  demodulates one packet's extent read from the flags byte, has no sync cap,
  correlates by FFT and computes the squelch envelope as a running sum: 40 of
  40 in 0.5 s, about a third of a core live. A burst also stops at its own
  packet (it used to run into the next slot, which was then parsed twice with
  the wrong time), and a false sync inside an extended payload is ignored.
- **The SDR finds weak and off-frequency packets.** Sync was found on the
  full-rate phase discriminator, which at the SNR where the matched filter
  still decodes is below the FM threshold -- so sync failed from ~15 dB
  symbol SNR down, and the carrier offset (every device's crystal error)
  tipped it over: at 23.7 dB a 9 kHz offset decoded 15%, 20 kHz 5-10%, and
  near threshold anything off frequency decoded 0%. A packet at exactly zero
  offset had been decoding by luck, through the no-sync fallback that
  assumes zero. Now: sync on a discriminator taken after an 8-sample
  low-pass; when that finds nothing in a run a packet long, a tone-energy
  sync over a grid of offsets (±40 kHz, 8 kHz steps, half-symbol windows);
  the offset measured by FFT of the known sync template multiplied out (the
  matched filter needs it within ~2 kHz: a residual f rotates the symbol
  integral by 2*pi*f*T); and the start searched ±1 block on the same tone
  sums as the rate. Paired 200-trial runs near threshold: at 20 kHz offset
  0% -> 20/73/88/96% at 7.2/8.5/9.7/10.7 dB (with repair); at zero offset
  30/74/94/97% against 32/84/95/97% before, where the old path was being
  told the right offset for free. 0 false accepts in 300 noise bursts; busy
  air still demodulates at ~2x real time. `cfo_hz` is now in the V4's
  packet records (and `snr_db`, which the monitor never set), which gives
  every device's crystal offset.
- **A repaired copy joins its message's event.** A damaged copy of another
  hop is too far in bits from the good copy to match on arrival (hop field
  and CRC differ), so it was repaired alone and emitted as a *second* event,
  numbered as a retransmission and heard by one receiver -- seen live on the
  V4 the first hour. Such buckets are now built first and merged into the
  message's bucket (or counted late if it has gone): `repaired_copies_folded`.
- **The dongle's RSSI stays out of the mesh.** It is a register snapshot taken
  after a 224 ms block has ended, so it mostly reads the floor (-104 dBm for
  a device decoded 96% of the time); in the mesh it sat beside the board's
  per-packet RSSI, broke the "closest" tie and filled the miss table's RSSI
  column. It stays in the pod's own log.
- **Listener firmware: capture sized to the slot grid, and no re-arm.** The
  next slot's 32-bit sync begins 424 bits after the FIFO starts, so a capture
  must stop short of one: 128 bytes ended inside slot 2 -- where the ACK to a
  one-hop message goes -- and the Heltec heard 36% of cleanup ACKs. 162 bytes
  holds slots 0-2 and stops 40 bits before slot 3. The loop also re-issued
  `SetRx` after every capture, restarting a continuous receiver that had
  already re-armed itself and dropping whatever had begun to arrive; and it
  now runs as a high-frequency loop so RxDone is seen within a millisecond.
- `deploy/Containerfile` had fallen behind the one actually built: it lacked
  the librtlsdr stage the V4 pod needs. Synced.

## 2.7.3 — 2026-09-23

Version numbers that mean something, and the listener board brought current.

- **One version, in one place.** It was written twice, in `pyproject.toml`
  and in `__init__.py`, and by 2.7.2 the two had drifted three releases
  apart: every pod reported 2.5.4 while running 2.7.2 code. That is exactly
  the question a deployment has to be able to answer about itself. The number
  now lives in `insteonrf/_version.py` -- a module with no imports, so
  setuptools reads it statically and the build never imports the package
  (and numpy, and rflib) to learn its own version -- and `pyproject.toml`
  takes it from there via `dynamic`. Tests refuse a second copy.
- **The Heltec was four days stale and now says so.** Its firmware was built
  2026-09-19 08:31; the last firmware commit landed at 08:37. Reflashed over
  the air. ESPHome's `build_time_str` only moves when the config hash
  changes, so the first reflash left the board still announcing the original
  timestamp -- it now carries `esphome.project.version`, published as a
  *Firmware Version* sensor, matching the Python package and pinned to it by
  a test.
- The first cut of that test read `pyproject.toml` with `tomllib`, which is
  standard library only from 3.11, so CI's 3.10 job failed on it while every
  other version passed. It parses the four lines it cares about as text now.
  Worth remembering when checking locally: CI runs `ruff check .` and bare
  `mypy` over the whole repo and tests on 3.10 through 3.14, all of which is
  wider than `ruff check insteonrf tests` on one interpreter.

## 2.7.2 — 2026-09-23

Two guards against decoding phantoms, after one of them spent eight days
looking like a neighbour's Insteon device.

`AC.4C.1E` and its family are not a device. All 82 clean decodes landed
within a second of one of this network's own messages (median 31 ms, 43 of
them inside 50 ms): they are second, false packets found inside the same
radio burst that carried a real one. A capture is longer than one packet --
a message, the start of its next hop, then padding -- and `parse_bits` looks
for the start header at every offset in either polarity, so a false lock in
that tail occasionally yields a short frame whose 5-bit counters descend
correctly *and* whose 8-bit CRC matches by luck. One in 256 per candidate,
against ~19,000 real messages a day each offering several offsets. They then
repeat forever, because the same byte pattern recurs.

- **`Packet.hops_ok`.** A device transmits with hops-left equal to max-hops
  and every repeater decrements hops-left, so hops-left can never exceed
  max-hops. Nothing checked the flags byte before. Enforced where a packet is
  admitted (`fusion`'s bucket and `_accept`), where one is repaired
  (`recover`, the one place a phantom can be *manufactured* rather than
  merely accepted), where one is counted (`MissTable`), and where one is
  converted for a protocol stack (`plm.to_plm_bytes`). Measured on 18,829
  messages from this network's real devices it rejects **none**, and it
  removes 18% of the phantom traffic outright. `hops_ok` is in `to_dict()`,
  so the logs show it.
- **An injection allowlist.** `--known-addrs FILE` names every address the
  network has, and `Injector.classify` refuses anything else. The remaining
  phantoms have plausible-looking flags, so structure alone will not catch
  them -- but an address that is not yours is proof enough. Eight days
  produced 61 such addresses over 234 messages, 13 of which are sitting in
  the miss table as devices the modem "misses" 100% of the time. Without the
  list injection is unrestricted, and the mesh command now says so loudly at
  startup.

`load_battery_addrs` is now `load_addrs`, since it reads both lists; the old
name still works.

## 2.7.1 — 2026-09-23

Two measurement bugs in the mesh, both found on 2026-09-21 and both fixed
here. Neither changed what was received; both changed what the numbers said
about it, and both would have misfired once injection was switched on.

- **The miss table counted the modem as a device.** `MeshService` accepted a
  `plm_addr` and the mesh command never passed one, so the exclusion that
  `MissTable` already implemented never ran: with no `--inject` there is no
  injector to borrow the address from. The modem therefore led its own report
  at a 100% miss rate over 2,936 messages, which is exactly backwards -- its
  transmissions are heard on RF and never come back as inbound messages -- and
  buried every real device under it. `--plm-addr` now reaches the miss table
  whether or not injection is on.
- **"The PLM missed this" was decided too early.** The verdict was taken when
  the fusion bucket closed, 0.6 to 2.0 s after the last RF sighting. The
  modem's copy of the same message routinely arrives later than that: it
  travels the powerline as well as the air, insteon-mqtt parses it, and only
  then republishes it. Measured against the modem's own log, it landed 0.7 to
  1.3 s after the first RF sighting for 116 of 587 messages -- about one in
  five of every "PLM MISSED" mark. A closed event now waits `PLM_GRACE_S`
  (2.5 s) before its verdict is settled, and `PlmMemory.heard()` is two-sided
  so a copy either side of the message counts.
- **Both lookups are anchored on the message, not the clock.** `heard()` is
  asked about the event's own time. Without that the new grace period would
  itself push every message out of its own suppression window, which is the
  more dangerous half of the same bug: the injector would hand insteon-mqtt
  messages the modem had already reported, and upstream's duplicate window is
  zero at no hops left.

## 2.7.0 — 2026-09-21

The deaf dongle, explained and fixed, and a third receiver on the air.

A day with all three listeners running made the dongle's drop-outs
measurable against the Heltec as a reference clock. It had been deaf for
about three quarters of the day, in spells that began the moment a busy
exchange ended: 75 listening spells since the previous midnight, a median
of 2.5 s and 11 messages each, separated by a median gap of 18 minutes and
a worst of 183.

**What it is not.** Not USB: the kernel logged no error, no disconnect and
no over-current on that port all day — every event on it was the watchdog's
own reset. Not quiet air: the Heltec had heard between 7 and 109 messages
in the ten minutes before every one of those resets, never zero. Not the
radio: with the pod stopped the dongle decoded 17 packets in 24 s at
914.950 MHz and 13 at 915.000, and nothing more than 50 kHz either side, so
it is correctly tuned and sensitive; carrier-detect capture worked too, and
host-side runs then heard 24 of 24 probes across four timed phases. Not the
frequency, the container's libraries (same pyusb both sides), or the RSSI
peek on its own (6 of 6 probes with it left in).

**What it is.** The receive path wedges mid-run while the chip still
answers register reads and still reports `MARC_STATE_RX` with a correct
modem configuration and a normal noise floor, and then hands over nothing
at all — not even the noise false-syncs. Caught live with a probe every
30 s: the pod heard 9 of 9, went deaf, missed 8, and came back on the tick
exactly five minutes after its last decode, which is when the old threshold
re-armed it. The best-supported mechanism is the one 2.6.1 already
suspected: the firmware's RF ISR drops a packet *without re-arming DMA*
when the main loop has not shipped the previous one, so whatever keeps the
host from draining EP5 during a burst costs the whole spell that follows.

- **Re-arm after 20 s of silence, not 30 minutes.** `--max-silence`
  defaults to 20 s and the pod passes it explicitly. A re-arm is a handful
  of register writes and can only lose a packet that is mid-flight.
- **The USB reset is no longer a step.** Over that day only 8 of 128 resets
  were followed by a decode inside a minute, the median wait for the next
  one was half an hour, and only 19% of listening spells began within 90 s
  of one — while the dongle was re-enumerated 200 times for nothing.
  `_silence_action` never returns `heal` now. A reset is reached only when
  the dongle has stopped answering, which the new `--diag-after` (default
  300 s) decides, and by `receive()` on repeated USB errors as before.
  Diagnostics are logged on that same slower timer rather than per action,
  which at a 20 s threshold would have been the whole log.
- **The decoder runs off the receive thread.** Publishing a capture,
  decoding with repair, the all-on watcher and the log write together take
  far longer than a block takes to arrive during a burst, and they sat
  between reads. They now run on a worker thread behind a bounded queue
  (`--queue`, default 512); the receive loop reads the radio, samples RSSI
  and hands the block over. A full queue drops a block and counts it rather
  than stalling the receiver — except on the `file` backend, which is
  marked `lossless` so replay still decodes identically whatever the queue
  size.
- **`read_rssi()` asked the dongle twice per block.** Written as a
  conditional expression, the `isinstance` test called `getRSSI()` and then
  the value called it again: two USB round-trips per block on the one
  thread that has to be draining the radio.
- **Two listeners could not share a broker.** `MqttPublisher` used the
  fixed client id `insteon-rf`, so the dongle pod and the V4 pod evicted
  each other in a reconnect loop the moment both were running. The id is
  now unique per process unless the caller names one.

- **Each receiver's SNR now reaches the fused record.** The V4 publishes
  symbol SNR with every capture and the mesh parsed it, then dropped it:
  `snr_db` was null on every event ever logged. It is now kept per receiver
  in `heard_by`, next to that receiver's RSSI and hop count, which is where
  a comparison between the three radios reads it.

**The V4 joins the mesh** (`deploy/insteonrf-v4.yaml`, receiver name `v4`).
It had never actually been deployed — it ran by hand once, for thirty
seconds, on 2026-09-19, which is why it appears in the logs as four events
and nothing since. It is the only receiver that produces soft decisions, so
fusion can repair a damaged copy from this radio alone. First minutes on
air: 3 to 5 captures per probe against 1 to 2 from each of the other two.
The image now builds the *blog* fork of librtlsdr (stock Osmocom does not
know the V4's R828D front end) with `DETACH_KERNEL_DRIVER=ON`, because the
kernel's DVB driver claims this device on sight and the host blacklist only
helps if those modules were not already loaded.

## 2.6.1 — 2026-09-19

The deaf dongle, second pass. Still not fully explained, but now measured
well enough to act on.

- **What a deaf spell looks like from inside.** `RfcatRadio.diagnostics()`
  reads MARCSTATE, RSSI, the firmware's trace codes (`getDebugCodes`) and
  the receive-path SFRs (DMAARM, DMAIRQ, RFIF, RFIM, RFST), and the
  watchdog logs it at start and around every action. First deaf spell
  caught with it: at start `MARC_STATE_RX`, RSSI −104.5, answering; five
  minutes later, still silent, **the dongle no longer answered any USB
  command** (`ChipconUsbTimeoutException` on a register read), and the
  watchdog's re-arm then crashed the process on `setModeIDLE`. After
  recovery the firmware's last exception code read
  `LCE_USB_EP5_TX_WHILE_INBUF_WRITTEN` — its EP5 IN path had been
  wedged. `receive()`'s own self-heal never fired because it wants USB
  *errors* alongside the timeouts, and a hung firmware produces neither.
- **Which reset works, measured.** A bus reset issued from *another*
  process while the pod still held the interface cured every deaf spell it
  was tried on (four of four, within seconds — twice `insteon-rf reset` on
  the host, once from a second process inside the container). The same
  libusb call from the pod's own process after releasing the interface
  cured none of four in-pod heals (15:31, 15:58, 16:12, and the 16:43 one
  that blocked past its 10 s timeout while rflib's reader thread sat in a
  bulk read on the same device). `dmesg` shows the kernel reset the port
  either way, so the difference is on the host side, in the libusb context
  that issues it. `heal()` now does what works: `external_usb_reset()`
  spawns a fresh interpreter to issue the reset *first*, while this process
  still holds the interface, then closes and reopens. The in-process reset
  stays only as a fallback when no child process can be started.
- **The watchdog escalates instead of crashing.** `diagnostics()` reports
  `answering: False` when a register read times out, and the silence tick
  then heals at once rather than re-arming and waiting another five
  minutes; a re-arm that raises heals too; a heal that raises is logged and
  retried on the next tick. Verified: the fresh pod comes up hearing after
  the change (two restarts), but no deaf spell has yet occurred *under* the
  new heal, so its effectiveness is inferred from the external reset it
  reproduces, not yet observed directly.
- **A deaf dongle is not silent, and that fooled the watchdog.** `py-spy
  dump --locals` on a deaf pod showed a block two seconds old: the radio
  still false-syncs on noise about once a minute and hands over junk
  (`1111…` right after the header — not Manchester). Liveness was "any
  block", so those kept the silence watchdog quiet indefinitely; it is now
  "a decoded packet", and the silence line reports how many undecodable
  blocks arrived meanwhile. So the picture of a deaf spell is: RX, right
  sync word, false syncs at the usual rate, never a real packet — which
  says tuned or timed wrong, not asleep.
- **`diagnostics()` dumps the modem as programmed** — frequency, data
  rate, deviation, bandwidth, sync word and the raw MDMCFG/MCSM/AGC/FSCAL
  registers — and `SIGUSR1` makes the monitor log it on demand (from the
  host: `kill -USR1 $(pgrep -f 'insteon-rf monitor')`; the pod image has no
  `kill`). Healthy baseline recorded; the next deaf spell gets diffed
  against it.
- **The afternoon's spells were self-inflicted, and that is the useful
  finding.** A `bpftrace` probe on `usb_reset_device` named the process
  behind every "mystery" reset: the test suite. `test_watchdog_heals_a_
  wedged_dongle` stubs `usb_reset` but, once `heal()` started resetting
  from a child process, the child was real — every `make check` since
  that change bus-reset the pod's dongle, the pod healed, and about half
  the time came back deaf. The fixture now stubs `external_usb_reset` too
  (0 resets during a full `make check`, verified with the probe). Lesson
  worth keeping: a test that reaches hardware another process is using
  looks exactly like a flaky device.
- **The reworked heal recovers from an external reset.** Reproduced on
  demand five times (`insteon-rf reset` from the host while the pod held
  the dongle): reset → `dongle not responding` → `back up` within 4 s →
  hearing on the next probe, five of five. The two deaf outcomes earlier
  in the afternoon (16:49, 17:02, both after test-suite resets on a
  pod under two minutes old) are not reproduced and not explained; the
  re-arm at five minutes cured one of them, so in the worst case the pod
  is deaf for five minutes, not an hour.
- `diagnostics()` also reads P1/P2/P2DIR/P2SEL and the amp mode: the
  Yard Stick One's RX/TX amplifier and bypass switches live on P2 and are
  set by the firmware on every mode change, outside the radio register
  file — the one thing a byte-identical modem dump cannot rule out. The
  next natural spell is logged with them.
- Not explained: why the pod came up deaf on roughly half its restarts
  while the same open sequence from the host never has, and what wedges the
  firmware's EP5 IN path mid-run. The firmware's RF ISR also has a branch
  that drops a packet without re-arming DMA when the main loop has not
  shipped the previous one (`LCE_DROPPED_PACKET`), a candidate for the
  "answering but silent" flavour seen earlier in the day.

## 2.6.0 — 2026-09-19

Soft decisions reach the mesh. Until now an I/Q receiver's per-symbol
confidence — the ~3 dB that `recover.py` has had since 2.2.0 — stopped at the
host that demodulated it: the capture payload carried bits only, and fusion
voted hard copies. Both ends now speak confidence, so the RTL-SDR V4 (or any
`rtlsdr`/`hackrf` backend) joins the mesh as more than another hard receiver.

- **Capture payload: optional `s` and `snr`.** `s` is base64 of one signed
  byte per bit of `b` (pad bits included), `+127` a clean `1`, `-127` a
  clean `0`, near zero a symbol the detector could not tell; `snr` is the
  demodulator's symbol SNR in dB. `Capture.from_payload` keeps a capture
  whose `s` does not match `b` in length as hard bits (with a warning) — the
  bits are still good, the confidence is not — and gives the restored sync
  header full confidence, since the host knows what it put back. Boards and
  the dongle publish neither field; `Capture.soft` is `None` for them.
- **`MqttPublisher.publish_capture` takes `soft=` and `snr_db=`**, and now
  ships every capture from just after the *first* header it finds, in
  either polarity, naming that header in `sw`. An SDR burst begins with
  preamble and its header can sit at any offset, so the old "strip a
  leading inverted header" only fitted the dongle; the consumer would have
  prepended a second header in front of the preamble. `monitor
  --mesh-capture NAME --backend rtlsdr` publishes bits, confidence and SNR.
- **Fusion votes with confidence.** `Sighting.soft` (aligned with
  `Sighting.bits`, sign-flipped with the polarity normalisation) is each
  copy's vote; a hard copy votes `±1`. `_vote` returns a `Vote` — bits,
  hard disagreements, and the per-position mean margin. When the vote
  still fails the CRC and a soft copy is present, the suspects are the
  `MAX_DISAGREEMENTS` least-sure positions inside the packet's extent,
  tried least-sure first, with any hard disagreement folded in; hard-only
  copies keep the old disagreement-driven search unchanged. **One soft copy
  alone is now enough to attempt repair** — the single-receiver case hard
  bits could never try — so a lone V4 capture damaged in two symbols comes
  out of the mesh as a `combined` event (`combined_from: 1`). Every
  candidate still has to pass CRC *and* the frame-index counters.
- **`MqttPublisher` says when the broker refuses it.** Everything it sends is
  QoS 0, and paho drops a QoS-0 publish made while disconnected without a
  word, so a refused login looked like "21 captures published" with nothing
  arriving (this broker rejects anonymous clients; the pods carry
  credentials, a host-side run needs `--mqtt-user`/`--mqtt-pass`). The
  connect callback now logs a warning naming the reason.
- Measured on the first V4 captures carrying `s` (12 captures, symbol SNR
  11–23 dB): inside a decoded packet at 22 dB, mean confidence 0.98 with
  every symbol above 0.9; at 11 dB, mean 0.68 with 23% of symbols below
  0.5 — the region where a confidence-ordered repair has something to
  work with and a hard receiver has nothing.
- Tests: confidence alignment and polarity flip, single-copy repair, a
  confident wrong symbol is *not* repaired from one copy, a hard copy
  outvotes a doubtful soft symbol, suspects tried least-sure first,
  end-to-end through `Fusion`, misaligned confidence dropped; payload
  parsing of `s`/`snr`, wrong-length and non-base64 `s`, and a round trip
  through the publisher from an SDR-style burst (preamble kept, header
  found, confidence re-aligned). 374 tests.

## 2.5.5 — 2026-09-19

The RTL-SDR Blog V4 that was lying around, plugged into the host with a bare
915 MHz antenna, no filter and no preamp. Getting it to decode *live* found
two bugs that had made the SDR backends useless on a real signal since 2.2.0;
file replay never exercised them.

- **`SdrReceiver.receive_burst()` spawned a new capture process per call.**
  A typo (`getattr(self, "_bit")` guarding `self._bursts`) meant every call
  created a fresh generator, whose first `next()` ran `_start()` again: a
  second `rtl_sdr` that could not claim the device, EOF, `exhausted = True`
  after one burst. `monitor`/`recv --backend rtlsdr` decoded one packet and
  went quiet. One generator per receiver now.
- **The numpy loop dropped samples on every packet.** Demodulation ran
  inline between pipe reads: one 56 ms burst takes ~85 ms to demodulate,
  and a Linux pipe holds 64 kB = 14 ms of I/Q at 2.4 Msps, so `rtl_sdr`
  lost the tail of everything (its "lost N bytes" goes to stderr, which is
  discarded). A reader thread now drains the pipe into a bounded queue
  (`QUEUE_SECONDS` = 4 s); if the demodulator ever falls that far behind,
  the drop happens in the queue where it is counted (`dropped_chunks`) and
  logged. Zero drops in the runs below.
- **`--demod` defaults to `numpy`; `auto` never picks the C demodulator.**
  `auto` chose `fsk2_demod` whenever it was built, and that binary fails on
  live signals (the 2.2.0 bench: no decodes even at 31 dB). It stays
  available by name for the regression fixture.
- **First live numbers, 80 s, gain 37.2, four Get Engine Version probes
  plus one incidental group broadcast (10 distinct messages), three
  receivers at the same spot:** messages heard — V4 8, dongle 9, Heltec 8;
  CRC-valid copies counting hop repeats — V4 28, dongle 19, Heltec 14. The
  V4 missed the two faintest things: a reply the dongle heard at −105.5 dBm
  and the group broadcast, where the simulcast repeats overlapped and the
  matched filter produced 10 fragments in one flush while the CC1111 locked
  on one copy and decoded hops 0, 1 and 2. The dongle missed the PLM's own
  probe to `29.4E.52` (a −72 dBm block it could not frame); the Heltec
  missed the `25.09.42` exchange entirely. Offline on 15 s captures the V4
  decodes 9 CRC-valid packets at symbol SNR 13–22 dB with squelch 12 at
  gain 37.2; at 49.6 the floor rises to 6.8 and the squelch chatters. So
  with no front-end filtering and an 8-bit ADC the V4 already matches the
  dongle on coverage and beats both on copies — and every receiver missed
  something another caught, which is the mesh's premise. Details in
  `Doc/MESH-PLAN.md` §3.7.
- **The dongle pod goes deaf, often, without erroring.** Its log had gaps
  of 27, 100, 52, 96, 51 and 20 minutes today while the Heltec, a few feet
  away, recorded 64–198 captures in each of them. The rfcat hears fine
  from the host during such a gap; the *pod* does not, and a pod restart
  did not cure it, while a host-side open/close of the dongle did once and
  `insteon-rf reset` (USB reset) did once, instantly. Not reproducible on
  demand — not by `rtl_sdr` streaming, by the SDR backends, or by
  importing rflib — and the gaps started before the V4 was plugged in.
  `heal()` is exactly that USB reset, so the pod now runs
  `--max-silence=300` (re-arm after 5 min, reset after 10). The repo's
  `deploy/insteonrf.yaml` was also behind the live manifest (no
  `--mesh-capture`); synced.
- Known, not fixed: `recover_packets()` returns nothing for two of the
  dongle's 2056-bit blocks that hold two packets each, although
  `parse_bits()` decodes both with CRC and frame counters intact —
  `find_headers()` reports a polarity flip and the decoder then reads
  Manchester garbage. The mesh and the CLI try `parse_bits` first, so no
  packet is lost today.

## 2.5.4 — 2026-09-19

- **Correction: no deployed receiver produces soft decisions.** The mesh
  plan, README, this changelog at 2.5.0, `monitor.py` and the mesh pod
  manifest all said the rfcat dongle was "the only receiver that can produce
  soft decisions". It cannot. The CC1111 is a hardware demodulator and hands
  over hard bits exactly as the SX1262 does; `cli._receive` has said so all
  along. `Burst.soft`, Manchester soft combining and the CRC-guided repair —
  the ~3 dB measured in 2.2.0 — come from I/Q samples, so today they exist
  only for the `rtlsdr`/`hackrf` backends and file replay, none of which is
  in the mesh. The dongle's real, measured advantage is a lower bit-error
  rate on this network: 0 of 10 first packets damaged against the Heltec's
  4 of 10 at the same spot. Getting soft decisions into the mesh needs an
  SDR front end and a capture payload that carries per-symbol confidence;
  neither exists yet. Caught when the claim was about to drive a hardware
  purchase.

## 2.5.3 — 2026-09-19

First contact: the Heltec LoRa 32 V3 received Insteon on the first flash.

- **The SX1262 syncs on Insteon in GFSK packet mode with the preamble
  detector off, at the default polarity.** A CRC-valid `2B.93.07 -> 29.4E.52
  Get Engine Version` at −92.5 dBm on the first probe. The core assumption
  the whole ESPHome path rested on is now measured, not argued.
- **The consumed sync header is put back on the host.** A radio consumes the
  pattern it synchronised on, so every capture arrives without its start
  header and the parser skipped the first packet — 3 of 4 first-run captures
  decoded truncated or not at all, with only the *next* hop repeat in the
  buffer still carrying a header. `with_sync_header()` prepends the low 16
  bits of the board's sync word (reported as `sw`, so a board flipped to the
  inverted word gets the matching header), exactly as `RfcatRadio.receive_bits`
  already does for the CC1111. The dongle's `--mesh-capture` publisher now
  strips its own prefix so both receivers ship the same shape: FIFO content,
  header restored by the consumer. Fixtures that included the header were
  not what hardware delivers and had hidden this.
- **Per-packet RSSI in firmware** via `GetPacketStatus` RssiAvg. `GetRssiInst`
  read after a capture measured whatever was on air next; a few feet from the
  PLM that was its hop repeat, so a distant device's ACK reported −46 dBm.
- **The dongle had been deaf for 2.5 days** (last decode 09-16 20:52) on a
  pod that predated the silence watchdog. Redeployed with it; this is the
  observed wedge the watchdog's earlier justification wrongly claimed.
- **Fusion across two real radios works**: one message, two receivers, one
  event, `closest` by hops-left with RSSI tiebreak, suppression correct.
- Open: in ~3 of 10 board captures the first packet's Manchester stream
  breaks 120–170 bits after sync while the dongle decodes the same packet
  whole. Leading hypothesis is bit-clock tracking of transmitters a few
  tenths of a percent off nominal baud (the SDR path grid-searches symbol
  rate for this reason); `preamble_detector_bits: 8` is the one-line A/B.
  Fusion recovered every such message from another copy in this sample.

## 2.5.2 — 2026-09-19

Pre-flash review of the ESPHome listener, with the Heltec LoRa 32 V3 in hand.

- **Fixed a blocker**: SX126x GFSK `SetPacketParams` takes nine bytes and the
  driver sent eight, leaving whitening undefined. Enabled whitening XORs every
  captured byte with PN9, so the Manchester gate rejects everything and a
  correctly wired board looks dead. The register the driver then poked to
  "disable whitening" (`0x06B8`) is the whitening seed, not an enable. Both
  corrected.
- The presence check now writes the sync word and reads it back. A status
  byte proves nothing: with no chip on the bus MISO floats high and reads
  0xFF. The readback also catches CS on the wrong pin, the likeliest wiring
  mistake, with a clear boot-time error instead of silence.
- Read the RX buffer from `GetRxBufferStatus`'s start pointer rather than
  offset 0; in continuous RX the chip advances the pointer between packets.
- IRQ polling is gated on the DIO1 level, so the idle loop no longer runs a
  4-byte SPI transaction at ~1 kHz against WiFi and MQTT.
- TCXO start-up delay arithmetic corrected (it was right for 5 ms by luck).
- RX gain boost register set; about 2 dB for about 2 mA on a mains listener.
- Bring-up hedges for the two unproven assumptions: `sync_word:` and
  `preamble_detector_bits:` are YAML options, the Manchester gate is now
  polarity-agnostic so a flipped sync word needs no other change, and the
  first ten captures after boot log at INFO with their leading bytes and the
  gate verdict.
- Pins verified against Meshtastic's `heltec_v3` variant: all seven match.
  Compiles clean for esp32-s3 under esp-idf. **Still never run on hardware.**
- **The on-board OLED is now used.** Previously nothing drove it (Vext was
  never switched on, so it sat dark). It shows the last real packet's RSSI
  large with a -110..-50 dBm bar, captures and accepts over the previous
  minute, the age of the last accepted packet, the lifetime count, and W/M
  flags for WiFi and MQTT -- upper case when connected. A radio that fails
  its boot-time readback shows `RADIO FAULT` instead. That is the placement
  survey without a laptop: carry the board and watch the bar. OLED pins from
  arduino-esp32's board definition (SDA 17, SCL 18, RST 21, Vext 36 active
  low). Two lambda mistakes caught by the compiler and worth remembering:
  a local named `rf` shadows `id(rf)`, and `id()` yields a pointer.

## 2.5.1 — 2026-09-15

- **`Doc/MESH-TRANSMIT.md`** — measured investigation of whether the listener
  boards could repeat for the PLM. They should not: Insteon repeating is
  synchronous simulcast on a **456-bit / 49.98 ms slot grid** (six half-cycles
  of 60 Hz), measured over 14 captures, with one transmission per slot and a
  pitch that does not vary with packet length. Joining a slot needs frequency
  agreement far tighter than the 75 kHz deviation, so an unlocked transmitter
  degrades exactly the marginal links it was meant to help. The failures on
  this network are inbound anyway (device ACKs at −103 to −110 dBm against the
  PLM's −51 to −68 dBm), and injection already handles that direction with no
  transmitter at all. Cheaper first moves: force `max_hops = 3`, and buy real
  dual-band range extenders, which simulcast correctly and bridge to
  powerline.
- **`monitor --max-silence`** re-arms the receiver after a long silence and
  USB-resets it after twice as long. rflib's own recovery needs receive
  timeouts *plus* USB errors, so a radio that has fallen out of RX while USB
  still answers looks healthy and just returns nothing — which the miss table
  would read as "the PLM misses nothing". Thresholds are long because this
  network carries about six RF messages an hour, so a quiet house and a broken
  radio are indistinguishable over any short window.
- The monitor exit summary reports published captures, re-arms and heals.

## 2.5.0 — 2026-09-15

More ears for the PLM. A PLM hears only what reaches its single antenna, and
passes up only the group broadcasts it holds an ALDB link for. 26 devices on
this network (3 mini-remotes, 21 water sensors, 2 door sensors) have no
powerline path at all and cannot be polled afterwards, so a message the modem
misses is lost for good. This release adds the path for extra receivers to
hand it what it missed. Plan and findings: `Doc/MESH-PLAN.md`.

Nothing here transmits. There is exactly one transmitter on an Insteon network
and it is the PLM.

- **`insteonrf/plm.py`** converts between RF packets and the `02 50`/`02 51`
  frames a modem hands its host. Group broadcasts **swap the two address
  slots** rather than encoding "group 00 00": the destination is a real
  `to_addr` whose low byte is the group, and an ALL-Link Cleanup Status Report
  (`cmd1 0x06`) carries the reported command in the high byte — `11.01.01` is
  "On, group 1". 27 of 223 live captures were such reports, and the first
  implementation zeroed those bytes. Found by replaying real captures through
  insteon-mqtt's own parser, which is how `tests/test_plm.py` checks it: 223 of
  223 captures round-trip, the one refusal being a packet truncated before its
  CRC.
- **`deploy/insteon-mqtt/`** patches insteon-mqtt with two topics, both off by
  default: `insteon/raw/rx` mirrors every inbound message *before* its
  duplicate check, and `insteon/raw/inject` feeds `Protocol.inject()`.
  Injection never touches the shared read buffer (it is filled by the serial
  link and may hold a partial frame), refuses anything that is not an inbound
  `0x50`/`0x51`, and reuses upstream's own duplicate check.
  `tests/test_inject.py` applies the series to a pristine tree and exercises
  the result, so a version bump fails there rather than on the running house.
- **`insteonrf/fusion.py`** folds hop repeats and multi-receiver copies into
  one event while keeping genuine retransmissions separate, attributes the
  copy closest to the source by hops-left (no clock synchronisation needed —
  the packet carries how far it travelled), and recovers packets no single
  receiver got by majority-voting across receivers. Combining votes over the
  whole capture, not `Packet.bits`: `decode_frames` stops at the first damaged
  Manchester pair, so a damaged copy's `bits` is truncated at exactly the
  region the others could have voted on.
- **`insteonrf/inject.py`** decides what may be handed over, and is built to
  say no: shadow mode by default, nothing allowed until a tier is enabled,
  never a reply (an ACK answers a command insteon-mqtt is waiting on), never
  the PLM's own transmission, never unverified bytes, and per-device plus
  global rate limits because inbound messages delay the modem's next transmit.
  Suppression keeps its own fixed window rather than trusting upstream's,
  which is `hops_left * 0.087` s and therefore **zero** at no hops left —
  measured live, the modem processed two copies of one ACK 87 ms apart.
- **`insteonrf/mesh.py`** and **`insteon-rf mesh`** run the service and
  produce the miss table that decides whether injection is worth enabling at
  all. The comparison works with injection off, which is the point. The modem
  is excluded from its own table: its transmissions are heard on RF but come
  back as `0x62` echoes, so they looked like the misses of a device that
  misses everything.
- **`monitor --mesh-capture NAME`** makes the rfcat dongle a mesh receiver, so
  the measurement needs no new hardware, and it stays useful once boards
  arrive as an independent second radio. (This entry originally claimed it
  produced soft decisions; it does not — see 2.5.4.)
- **`esphome/components/insteon_rf/`** is the listener firmware: a hand-rolled
  SX126x driver (no external library, builds under esp-idf) using GFSK packet
  mode as a raw bit recorder, because SX126x dropped the continuous mode the
  SX127x family has. Preamble detector off — Insteon's preamble is a repeating
  `0110` cell, not the `0x55` alternation the detector expects — with a
  Manchester-validity gate in its place, since 26 of every 28 on-air bits are
  Manchester pairs and noise fails within a handful. Compiles clean for
  esp32-s3; **never run on hardware**.
- **`tools/gen_sync_word.py`** derives the sync word from `Packet.to_bits()`
  instead of by hand, and `tests/test_sync_word.py` pins it. It comes out as
  `0x33333155`, whose low half is the `0x3155` the CC1111 dongle already syncs
  on — an independent check that the polarity and phase are right. A wrong
  sync word is the worst kind of firmware bug: the radio never matches and
  reports nothing, which looks exactly like a quiet house.
- **`deploy/insteonrf-mesh.yaml`** runs the service as a second pod with no
  USB, so it restarts freely while the dongle pod keeps its privileged access.

## 2.4.2 — 2026-09-13

- **`monitor --mqtt-alerts-only`** publishes only all-on triggers, to
  `<topic>/alert` at QoS 1, instead of every packet. Unsubscribed messages do
  not accumulate on a broker — unretained QoS 0 publishes are dropped, and the
  persistence file only holds retained and queued QoS 1+ messages — but a
  broker configured with `log_type debug` writes a line per publish, so a
  per-packet stream with no subscriber costs log volume and nothing else. The
  rotating JSON-lines file was always the durable record; MQTT now only carries
  what needs a live consumer. `--mqtt-retain-alerts` keeps the last alert for a
  consumer that connects later, at the cost of re-firing on reconnect.
- The pod manifest uses it, and reports published/alert counts at exit.

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
