# More ears for the PLM — distributed Insteon RF receive plan

**Goal (set 2026-09-15):** make insteon-mqtt more sensitive and more accurate
by giving it additional receive antennas around the house. Messages that the
PLM's single radio missed get decoded elsewhere and injected into
insteon-mqtt's inbound path as if the PLM had heard them.

Everything flows through insteon-mqtt's own protocol layer — no parallel
entities, no fabricated HA state, no refresh-polling. insteon-mqtt already
knows how to handle every message type, correlate ACKs, apply scenes and
update HA. It just cannot hear well enough.

**Decisions taken:** patch insteon-mqtt (rather than tail its log or infer from
state topics); Heltec LoRa 32 V3 / SX1262 as the receiver hardware, no separate
hardware-feasibility phase; one board to start.

---

## Status (2026-09-15)

Phases 1-3 are **built and running**; 4-7 are **written and unit-tested but
not proven on air**. What is deployed:

| | |
|---|---|
| `insteon` sidecar | patched image `localhost/insteon-mqtt-raw:local`, publishing every inbound message to `insteon/raw/rx`. Injection compiled in but **disabled** in `config.yaml`. |
| `insteonrf` pod | now also publishes raw captures to `insteon-rf/rx/dongle`, so the rfcat dongle is the mesh's first receiver. |
| `insteonrf-mesh` pod | fuses captures, compares against `insteon/raw/rx`, writes `/nvme/churn/insteonrf-mesh/log/insteon-rf-mesh.jsonl` and an hourly miss table. **No `--inject`.** |
| `insteonrf-v4` pod | the RTL-SDR Blog V4 on `insteon-rf/rx/v4` (2026-09-21). Three radios on the air, and the only one of them producing soft decisions. |

Verified end to end on live traffic: a benign Get-Engine probe produced
captures on `insteon-rf/rx/dongle`, matching frames on `insteon/raw/rx`, fused
events with `heard_by`/`closest`, and a miss table correctly marking the
device ACKs as also-heard.

Not yet proven: that an SX1262 can sync on Insteon at all (§3.1), and anything
that needs more than one receiver — cross-receiver combining and RSSI
localisation are implemented and unit-tested against synthetic copies, but
have never run on two real radios.

**Pre-flash review (2026-09-19, board in hand).** Pins verified against
Meshtastic's `heltec_v3` variant — all seven match, TCXO 1.8 V on DIO3,
DIO2 as RF switch, DC-DC. Re-reading the driver found one blocker that would
have made a correctly wired board look dead: `SetPacketParams` for GFSK takes
**nine** bytes and the code sent eight, leaving *whitening* undefined (and the
register it then poked, `0x06B8`, is the whitening seed, not an enable).
Also fixed: a presence check that a floating MISO would pass, the TCXO delay
arithmetic, reading the RX buffer from offset 0 in continuous mode instead of
`GetRxBufferStatus`, and IRQ polling every loop iteration (now gated on DIO1).
Added for bring-up: RX gain boost, `sync_word:` and `preamble_detector_bits:`
in YAML so the two unproven assumptions can be flipped over OTA, a
polarity-agnostic Manchester gate so a flipped sync word needs no other
change, the first ten captures logged at INFO with their leading bytes, and
the on-board OLED driven at last (RSSI bar, cap/ok per minute, age of the
last packet, WiFi/MQTT flags) so the placement survey needs no laptop.
Compiles clean; still never run on hardware.

**First contact (2026-09-19, later the same day).** Flashed a few feet from
the PLM and the dongle. It received Insteon on the first probe: a CRC-valid
`2B.93.07 -> 29.4E.52 Get Engine Version` at −92.5 dBm. Packet-mode sync with
the preamble detector off works, and the default polarity is right — §3.1's
open question is closed. Two things the first hour taught: the radio
*consumes* the sync word, so captures arrive headless and the host must put
the header back (`with_sync_header`, mirroring what the rfcat path already
did); and `GetRssiInst` read after a capture is not the packet's RSSI (now
`GetPacketStatus` RssiAvg). With both fixed, the mesh pod fused one message
from both radios into one event with correct `closest` and `plm_saw_it`.
Open: ~3 in 10 board captures break their Manchester stream 120–170 bits
after sync on packets the dongle decodes whole; suspected bit-clock tracking
of off-nominal transmitters, A/B via `preamble_detector_bits: 8`.

### Findings that changed the design

* **Group broadcasts swap the address slots; they do not encode "group 00 00".**
  27 of 223 live captures were ALL-Link Cleanup Status Reports whose
  destination carries the reported command in its high byte (`11.01.01` = "On,
  group 1"). The first implementation zeroed those bytes. Caught by replaying
  real captures through insteon-mqtt's own parser (§5).
* **insteon-mqtt is pip-installed into site-packages**, and `/opt/insteon-mqtt`
  is only the source checkout. Patching `/opt` alone produced an image that
  looked patched and behaved like stock. Worse, the build-time check passed
  because it forced `sys.path` at the patched tree. Both now fixed, and the
  check imports the package the way the service does.
* **insteon-mqtt validates its config against a cerberus schema** and rejects
  unknown keys under `mqtt:` (they are parsed as user-defined discovery
  classes). Adding the `raw:` section without patching the schema crash-looped
  the sidecar and took Insteon down until the config was reverted. The image
  now fails the build instead.
* **The modem already double-processes some hop repeats.** Two copies of one
  ACK arrived at hops 1 and 0, both `dup: false`, 87 ms apart — its window is
  `hops_left * 0.087` s, which had already expired. Confirms that
  injector-side suppression cannot be delegated upstream (§6.4).
* **The PLM's own transmissions are heard on RF but never return as inbound
  messages** (they come back as `0x62` echoes). They therefore look like the
  misses of a device that misses everything, and led the first live miss table
  at a 100% rate. Excluded by address, and never injectable.
* **`Packet.bits` is the wrong substrate for combining.** `decode_frames`
  stops at the first damaged Manchester pair, so a damaged copy's `bits` is
  truncated at exactly the region the other receivers could have voted on.
  Combining votes over the whole capture instead (`sightings_from_capture`).
* **Upstream's `Signal.connect` holds weak references**, so a lambda slot is
  collected the moment `connect()` returns and the signal silently does
  nothing.

---

## 1. Start with the hardware already running

The `insteonrf` pod and its rfcat dongle are **already a second pair of ears**,
running receive-only with a validated decoder. Every part of this plan except
the ESPHome firmware can be built and proven against it, today, with no new
hardware:

- the insteon-mqtt patch (§4)
- RF → PLM wire-format conversion (§5)
- the injector, its allowlist and its rate limits (§6)
- the miss-rate measurement that decides whether any of this is worth it (§7)

So the Heltec is **receiver #2**, not receiver #1. That reorders the phases
usefully: the expensive question ("does the PLM actually miss anything?") gets
answered before any firmware is written, and the firmware then plugs into a
path that already works.

It also means the dongle stays in the architecture permanently. Not for soft
decisions — an earlier draft claimed that, wrongly: the CC1111 is a hardware
demodulator and hands over hard bits exactly as the SX1262 does, and soft
decisions exist only on the I/Q path, which no deployed receiver uses yet
(§3.3). It stays because it is a proven, independent second radio, and on the
first day it decoded whole packets the Heltec flipped bits in.

---

## 2. What this changes and what it cannot

**It can:**

| | |
|---|---|
| Recover messages from the 26 **RF-only battery devices** | 3 mini-remotes, 21 water sensors, 2 door sensors. No powerline path at all — 24 have never even been ALDB-cached. These cannot be polled after the fact: a missed water-sensor alert is lost permanently. Injection is the only mechanism that can help them, which makes them both the highest-value and the safest first target. |
| Recover dual-band device events the PLM missed | Button presses, local load changes, scene triggers that never reached the PLM, so HA never updated. |
| Make group broadcasts visible that the PLM *filters* | A PLM only passes up group broadcasts it holds an ALDB link for. An unlinked group-0 broadcast — the phantom all-on — never reaches the host at all. An injected copy does. |
| Improve accuracy, not just coverage | insteon-mqtt's `set_wait_time()` uses inbound hop counts to decide when the air is clear. More inbound visibility means better transmit timing, not just more state. |

**It cannot:**

- **Improve delivery *to* devices.** These nodes are receive-only, permanently.
  A second transmitter would collide with the PLM. The PLM owns the air.
- **See powerline-only traffic.** Insteon RF carries only what dual-band
  devices put on the air. "Device never heard on RF" is usually correct, not a
  bug.
- **Fix a device that is failing to transmit.** If a sensor's radio is dead or
  its battery is flat, more receivers change nothing.

---

## 3. Receiver hardware

### 3.1 The SX1262 approach

The SX1262 has no continuous / raw-bitstream mode — the SX127x family exposes
DATA and DCLK pins for bit-level access and SX126x dropped it. So the firmware
abuses **GFSK packet mode as a raw bit recorder**:

| Setting | Value | Reason |
|---|---|---|
| Modulation | GFSK, no shaping | Insteon is plain 2-FSK |
| Frequency | 914.95 MHz | matches `radio/rfcat.py` |
| Bitrate | 9124 bps | Insteon symbol rate |
| Deviation | 75 kHz | Insteon deviation |
| RX bandwidth | 234.3 kHz | nearest discrete step above 2·75 + 9.1 kHz |
| Sync word | 24–32 bits, generated from `Packet.to_bits()` (§3.2) | locks the frame grid |
| Preamble detector | **off** | Insteon's preamble is a repeating `0110` cell (0x66), not the 0x55/0xAA alternation the SX126x detector expects |
| CRC | off | Insteon's CRC is its own algorithm |
| Address filter | off | Insteon addresses are not where the chip looks |
| Whitening | off | would destroy the payload |
| Packet length | **fixed**, 128 bytes | Insteon has no length field the chip can read |

The packet engine is a shift register feeding a 256-byte FIFO; it does not care
that the payload is Manchester-coded with interleaved frame counters. It hands
over raw on-air bits — exactly what the host pipeline already consumes ("one
burst per line of `0`/`1` characters").

128 bytes = 1024 bits = **112 ms** of air. Longer than a standard packet
(13 frames × 28 bits = 364 bits = 40 ms) and longer than an extended one
(32 frames = 896 bits = 98 ms), so a capture usually holds the tail of one
transmission plus the start of the next hop repeat. That is fine —
`parse_bits()` finds every packet in a bit string at any offset and in either
polarity. Make the length configurable so it can be tuned.

Per the decision above there is no separate feasibility phase. The first commit
of Phase 4 is still "sync and dump a FIFO", and if the packet-mode trick does
not work that shows up within an afternoon; §9 records the contingency.

### 3.2 Generating the sync word (do not hand-derive it)

The on-air stream is inverted, LSB-first, Manchester-coded, and the first frame
is data-independent — literal `11` then Manchester(index 31 = `11111`) —
preceded by the repeating `0110` preamble. That is a long invariant run, but
deriving the exact bytes by hand is how a sign error gets baked into firmware.

Generate the constant from the code that already talks to real devices:

```python
# tools/gen_sync_word.py
bits = Packet(...).to_bits()      # validated against hardware
print(hex(int(bits[preamble_end:preamble_end + 32], 2)))
```

`tests/test_sync_word.py` then asserts the constant still matches
`to_bits()`, so firmware and host cannot drift. Emit **both polarities** and
pick empirically — the CC1111 syncs on the inverted start header `0x3155`,
which says polarity is deterministic on a real receiver, but confirm rather
than assume.

The SX1262 supports only **one** sync word. If both polarities turn out to
occur, fall back to a preamble-only sync word (repeating `0x66`, which matches
the inverted stream too, just at a 2-bit offset) and resolve polarity in
software, accepting more false syncs for §3.4 to absorb.

### 3.3 Soft decisions: only from I/Q, and now in the mesh (2.6.0)

Both boards-class receivers give hard bits — the SX1262, and the CC1111 in
the rfcat dongle too. `Burst.soft` and everything `recover.py` builds on it
— Manchester soft combining, the CRC-guided bounded repair, the ~3 dB
measured in 2.2.0 — come from I/Q samples, so they exist only for the
`rtlsdr`/`hackrf` backends and file replay. An earlier draft of this plan
said the dongle had them; it did not.

Since 2.6.0 the confidence travels: `monitor --backend rtlsdr --mesh-capture
v4` publishes `s` (one signed byte per bit, `±127` = a clean symbol) and
`snr` alongside `b`, `Capture.soft` carries it into `Sighting.soft`, and
`combine()` votes with it. Three consequences:

- **A hard copy and a soft copy combine sensibly.** Each copy votes with its
  confidence — the dongle's bits at `±1`, the V4's at whatever the matched
  filter measured — so a symbol the V4 was unsure of loses to one the dongle
  was sure of, and vice versa where the dongle's bit is the lone dissenter
  against a confident V4 symbol.
- **When the vote still fails the CRC, the suspects are the least-sure
  positions**, inside the packet, tried least-sure first. That covers both a
  disagreement between receivers (its margin is small) and a symbol the I/Q
  receiver itself flagged. Hard-only groups keep the disagreement-driven
  search of 2.5.0 unchanged.
- **One soft copy alone can be repaired.** Hard copies need two to have any
  suspects at all; a lone V4 capture damaged in two symbols comes out as a
  `combined` event with `combined_from: 1`. This is the local repair the
  `monitor` already did on the host, now done in fusion where the result
  can also be checked against what the PLM heard.

What does not change: spatial diversity is still worth more than 3 dB for
"did anyone hear it", every candidate still needs CRC *and* frame counters,
and the V4's weakness in §3.7 — overlapping simulcast copies that the
matched filter cannot frame — produces no capture at all, so there is no
confidence to vote with. The dongle's sync correlator remains the better
receiver for those.

### 3.4 The on-board validity gate

915 MHz ISM is crowded (utility meters, weather stations, LoRa). With the
preamble detector off, false syncs will be frequent.

The gate is cheap and devastatingly effective: **Manchester validity**. Roughly
26 of every 28 on-air bits are Manchester pairs, and a valid pair is only `01`
or `10` — never `00` or `11`. Noise fails within a handful of bits.

```
for each of the first `manchester_gate` frames (28 bits each):
    bits[0:2] must be the literal '11'  (inverted on air: '00')
    the 5-bit index field: 5 pairs, each 01 or 10
    the 8-bit data field:  8 pairs, each 01 or 10
    frame 1's decoded index must be 31
    frame n's decoded index must be 11-(n-2) (standard) or 30-(n-2) (extended)
reject the capture if more than k violations
```

About 40 lines of C++ and no full Manchester decode — just pair validity plus
the index fields. This is the "thin but not dumb" line: the board does sync
detection and a structural sanity check; all protocol knowledge (CRC, frame
indexes, repair, command tables, ACK correlation) stays in the Python package
where it is already tested.

Expose `captures_per_minute` and `accepted_per_minute` as separate sensors so
the rejection ratio is visible. It is the main tuning knob and the first thing
to check when a node goes quiet or goes noisy.

### 3.4b Next board: ESP32 + CC1101 (researched 2026-09-19)

The SX1262's packet engine imposes a sync word, a fixed length, a consumed
header and one polarity; the CC1101's **asynchronous serial mode** imposes
none of them — it streams the demodulated bits on a GDO pin and the host does
all framing. It is also the same demodulator family as the rfcat dongle,
which on the first day decoded whole packets the Heltec flipped bits in.

**The only mainstream single board is LILYGO's T-Embed CC1101** (and the
"Plus", which just adds an nRF24L01). ESP32-S3-WROOM-1, 16 MB / 8 MB PSRAM,
1.9" ST7789 170×320, rotary encoder, 1300 mAh, IR, NFC, microSD, Qwiic.
Verified from the vendor repo:

```
CC1101  CS 12  SCK 11  MOSI 9  MISO 10  GDO0 3  GDO2 38
Antenna switch  SW1 47  SW0 48   ->  SW1:0 SW0:1 selects the 868/915 path
Display ST7789  CS 41  DC 16  BL 21    Encoder 4/5/0   Button 6   RGB 14
```

GDO0/GDO2 both reach ESP32 GPIOs, so the raw bit stream is capturable, and
915 MHz has its own matching path rather than a wideband compromise. Buy the
**external-antenna (SMA) version** and fit a 915 antenna.

**Firmware may need no C++ at all.** ESPHome ships an official `cc1101`
component: `modulation_type: 2-FSK`, `symbol_rate: 9124`,
`fsk_deviation: 75kHz`, `filter_bandwidth: 203kHz` (the CC1101 step the
rfcat config uses as "200 kHz"), `frequency: 914.95MHz`, `manchester: false`,
plus AGC controls (`freeze: On Sync`, `rx_attenuation`) that bear directly on
the simulcast/AGC question. Its default **async mode** puts the demodulated
line on GDO2 for `remote_receiver`, i.e. RMT edge capture at ~1 µs against a
110 µs symbol — timing jitter per symbol is a real, if crude, confidence
signal. Its packet mode caps at 64 bytes (the chip FIFO), too short for an
extended packet, so async is the path. Expect to tune `remote_receiver`
`idle` (~2 ms) and `buffer_size` so a whole burst is one event, and to turn
edge timings into the capture payload either on-board or on the host.

Compact no-display alternative: hallard's open-hardware **ESP32C3-CC1101**
(ESP32-C3 + Ebyte E07-900M10S, a 915-band-specific module, u.FL) — a PCB
design to have made, not a product; GDO0 IO1, GDO2 IO0. Cheapest of all: an
ESP32-S3 SuperMini plus a bare CC1101 module, seven wires, for permanent
hidden placement near the water sensors.

### 3.5 Placement of the first board

One board. Put it **where the PLM is weakest**, which is by definition not near
the PLM. Two candidate strategies, decide with data from Phase 3's miss map:

- Wherever the water sensors cluster (basements, laundry, under sinks, water
  heaters) — the devices with no second chance.
- Near the known-marginal loft KPL `2B.A0.AB`, which answers at about
  -102 dBm.

The onboard OLED earns its keep during the survey: show last-heard address,
RSSI and a packets/min counter so the board can be walked around and watched.

Mount the antenna **vertical** (Insteon devices are vertically polarized in
their wall boxes), at least 30 cm from ductwork, panels and mirrors.

### 3.6 Deliberately not doing

- **No board-to-board communication.** Star topology to MQTT; all fusion
  central. No ESP-NOW, no LoRa meshing. Nothing needs it, and §6.4 explains
  why no clock synchronization is needed either.
- **No BLE proxy on these nodes**, despite the house pattern and Bermuda
  wanting more receivers. WiFi + BLE + continuous SPI FIFO service on one
  ESP32-S3 risks interrupt latency that drops captures. Revisit only after the
  RX path is stable for weeks.
- **No transmit. Ever.** The firmware should not even link a TX path.

---

### 3.7 RTL-SDR V4 on the host: first light (2026-09-19)

An RTL-SDR Blog V4 with a bare 915 MHz antenna — no SAW filter, no LNA — on
`alpha` next to the rfcat dongle, a few feet from the Heltec and the PLM.
Driver: RTL-SDR Blog's librtlsdr fork (the V4's R828D needs it), kernel
`dvb_usb_rtl28xxu` blacklisted. `insteon-rf monitor --backend rtlsdr --gain
37.2 --demod numpy`, 2.4 Msps (~263 samples per bit), squelch 12.

**Live, 80 s, four Get Engine Version probes plus one incidental group
broadcast: 10 distinct messages.** Decoded with `parse_bits`, the decoder the
mesh uses; hop-counter values in brackets are the copies each receiver
framed with CRC and frame counters intact:

| message | V4 | dongle | Heltec |
|---|---|---|---|
| PLM → 29.4E.52 Get Engine | [1, 2] | – | [0, 1] |
| 29.4E.52 → PLM reply | – | [0] (−105.5 dBm) | [0] (−97) |
| group broadcast On (29.41.B5) | – (10 fragments) | [0, 1, 2] | [1, 2] |
| PLM → 29.41.B5 On | [0, 1] | [0, 1] | [0] |
| PLM → 25.09.42 Get Engine | [0, 1] | [0] | – |
| 25.09.42 → PLM reply | [0] | [0] | – |
| PLM → 2B.A0.AB Get Engine | [0, 1, 2] | [0, 1] | [0, 1] |
| 2B.A0.AB → PLM reply | [1] | [1] | [0, 1] |
| PLM → 29.4D.F8 Get Engine | [0, 1, 2, 3] | [0, 1, 2] | [0, 1, 2] |
| 29.4D.F8 → PLM reply | [2] | [0, 1] | [2] |
| **messages heard** | **8** | **9** | **8** |
| **CRC-valid copies** | **28** | **19** | **14** |
| undecodable | 17 fragments | 3 blocks | 2 captures |

What the table says:

- With no front-end filtering and an 8-bit ADC the V4 already matches the
  dongle on coverage and produces the most copies — which is what
  cross-receiver combining (§6.6) feeds on.
- Its two misses are instructive. The `29.4E.52` reply was the faintest
  thing in the window (the dongle read −105.5 dBm, its idle floor). The
  group broadcast was heard by everyone *except* the V4, which produced
  ten fragments in one 0.5 s flush: the simulcast repeats from several
  dual-band devices overlapped, and the matched filter — which assumes one
  signal — could not frame any of them, while the CC1111's sync-word
  correlator locked onto one copy and decoded hops 0, 1 and 2. Handling
  overlapping copies is a demodulator problem, not a hardware one.
- The dongle missed the PLM's own probe to `29.4E.52` from a few feet away
  (a −72 dBm block it could not frame — most likely a collision), and the
  Heltec missed the whole `25.09.42` exchange. Every receiver missed
  something another caught. That is the premise of the mesh, now measured
  with three different front ends.
- Offline, on 15 s captures: 9 CRC-valid packets at symbol SNR 13–22 dB at
  gain 37.2; at gain 49.6 the noise floor rises from ~2.7 to 6.8 (|IQ|)
  and squelch 12 chatters, so 37.2 is the setting here.

**What it cost to get here.** Two bugs made the SDR backends useless live
since 2.2.0 (file replay never hit them): `receive_burst()` spawned a new
`rtl_sdr` per call, and the numpy loop demodulated inline between pipe
reads — one 56 ms burst takes ~85 ms to demodulate and the pipe holds 14 ms,
so `rtl_sdr` silently dropped the tail of every packet. A reader thread now
drains the pipe into a 4 s queue (0 drops in these runs). Also `--demod auto`
picked the C demodulator, which fails on real signals; `numpy` is the
default. See the 2.5.5 changelog.

**Resolved 2026-09-21: the dongle wedges, and a reset was the wrong cure.**
A day with all three receivers made it measurable. The dongle was deaf for
about three quarters of the day in spells that began the moment a busy
exchange ended (75 spells, median 2.5 s of listening and an 18 min gap),
while still answering register reads and still reporting `MARC_STATE_RX`
with correct registers and a normal noise floor. It is not USB, not the air
and not the radio — stopped, it decodes 17 packets in 24 s on frequency.
Only 8 of 128 USB resets were followed by a decode inside a minute, so the
reset escalation is gone; the threshold is now 20 s and the action is always
a re-arm. The suspected mechanism is the one below: the firmware's RF ISR
drops a packet without re-arming DMA when the host has not taken the
previous one, so the monitor's per-block work (capture publish, decode,
watcher, log write) now runs on a worker thread and the receive loop does
nothing but drain the radio. The Heltec did become the reference clock this
was measured against, as the note below hoped. Superseded detail follows.

**A finding about the dongle, not the V4.** Comparing the three logs showed
the dongle *pod* deaf for most of the day: gaps of 27, 100, 52, 96, 51 and
20 minutes in its log while the Heltec recorded 64–198 captures in each
gap. During a gap the rfcat hears fine from the host; a pod restart did not
cure it; a host-side open/close of the dongle did once and `insteon-rf
reset` did once, instantly. Nothing reproduces it on demand — not
`rtl_sdr` streaming, not the SDR backends, not importing rflib — and the
gaps began before the V4 was plugged in. `heal()` is that same USB reset,
so the pod now runs `--max-silence=300`: re-arm after 5 min of silence,
USB-reset after 10. A spurious reset on a quiet night costs ~3 s of
listening. Whether the watchdog actually recovers it will show in the pod
log (`healing the radio` / `dongle back up`) and in the next day's gap
list; if it does not, the Heltec is the better reference clock for
"is the air quiet or is the dongle deaf" and the watchdog should ask it.

**The V4 in the mesh:** `monitor --backend rtlsdr --mesh-capture v4`
publishes its bursts like any board, plus per-symbol confidence and SNR
(§3.3, 2.6.0). It is the receiver with the most copies at this spot and the
only one fusion can repair on its own. Adding the Uputronics filter/preamp
(§3.4b) is the next hardware step; the collision behaviour above will not
change with it.

## 4. The insteon-mqtt patch

Runs as the `insteon` native sidecar in the `homeassistant` pod, image
`td22057/amd64-insteon-mqtt:latest`, source at `/opt/insteon-mqtt`.

### 4.1 What the existing code already gives us

Reading `Protocol.py` and `message/InpStandard.py` settles most of the design:

- `_data_read(link, data)` accumulates into `self._buf`, finds `0x02`, looks up
  `Msg.types[msg_type]`, calls `msg_class.from_bytes()`, then:
  `if self._is_duplicate(msg): ... else: self._process_msg(msg)`.
- **`_is_duplicate()` already ignores hops.** `InpStandard.__eq__` compares
  `from_addr`, `flags`, `group`, `cmd1`, `cmd2` and explicitly excludes
  `hops_left` and `max_hops` — independently the same dedup key this repo's
  `monitor.Deduper` arrived at.
- The window is `expire_time = now + hops_left × 0.087` for standard and
  `× 0.183` for extended. Those constants are per-hop air time and match the
  measured 40 ms / 98 ms packet durations plus powerline propagation.

So **injected messages are deduplicated against the PLM's own copies for
free**, provided they arrive inside the window.

### 4.2 Two traps

1. **Never append to `self._buf`.** It is shared with the serial link. An
   injected frame landing between two halves of a real PLM frame corrupts
   both streams. The injection point is: parse our own bytes into a `Msg`,
   then `_is_duplicate()`, then `_process_msg()` — bypassing the buffer
   entirely.
2. **`_is_duplicate()` calls `set_wait_time(msg.expire_time)`**, so every
   inbound message pushes out insteon-mqtt's next allowed transmit. Injection
   therefore throttles outbound commands. Mostly this is correct — we are
   telling it the air is busy — but a flood would stall the network. It is a
   hard argument for the rate limits and the allowlist in §6.

### 4.3 Shape of the patch

Small, idiomatic, confined:

- **`Protocol.inject(raw: bytes) -> bool`** — new public method. Parses via the
  existing `Msg.types` lookup, applies `_is_duplicate()`, calls
  `_process_msg()`, returns whether it was accepted. Logs at INFO with an
  `injected` marker so the log stays greppable.
- **`Protocol`: publish inbound** — in `_data_read`, after a successful
  `from_bytes` and **before** the dedup check, emit the raw hex plus msg type,
  decoded `str(msg)`, and whether it was a duplicate. This is the ground truth
  for "what did the PLM actually hear", and publishing pre-dedup means hop
  copies are visible too.
- **`insteon_mqtt/mqtt/Raw.py`** — new module beside the existing mqtt
  handlers, owning two topics and reusing the broker connection that
  `network/Mqtt.py` already holds (do **not** open a second client):

  | Topic | Direction | Payload |
  |---|---|---|
  | `insteon/raw/rx` | out | `{"ts":…,"code":80,"raw":"0250…","str":"…","dup":false}` |
  | `insteon/raw/inject` | in | `{"raw":"0250…","src":"insteon-rf-up","rssi":-87}` |

- **Config** — `raw_topics: enabled/inject_enabled` in `config.yaml`, both
  defaulting to off, so the patch is inert until deliberately switched on.

Roughly 100 lines including config plumbing and tests.

### 4.4 Carrying the fork

The image is `:latest`, so an unpinned pull would silently discard the patch.

- Pin the base image to a **digest**, not a tag.
- Build locally with the pattern already used for `nfhs-relay` and `insteonrf`:
  `buildah bud` → `buildah push docker-archive:` → `microk8s ctr image import`
  → `imagePullPolicy: Never`.
- Keep the change as a **git patch series** applied at build time rather than
  edited files, so a version bump shows conflicts instead of quietly reverting.
- Containerfile and patches live in `/nvme/k8s/insteon-config/build/`, matching
  the `nfhs-relay` layout.
- Upstream the raw-topic feature if it proves out. It is generally useful and a
  merged feature is better than a carried patch.

---

## 5. RF → PLM wire format

New `Packet.to_plm_bytes()` in `insteonrf/packet.py`.

| | RF on-air order | PLM inbound frame |
|---|---|---|
| Standard | `flags, to(3), from(3), cmd1, cmd2, crc` | `02 50 <from 3> <to 3> <flags> <cmd1> <cmd2>` |
| Extended | `flags, to(3), from(3), cmd1, cmd2, data(13), dcrc, crc` | `02 51 <from 3> <to 3> <flags> <cmd1> <cmd2> <data 14>` |

Three things to get right:

1. **Field order differs** — RF leads with flags and puts `to` before `from`;
   the PLM frame leads with `from`. A remap, not a copy.
2. **Group broadcasts are special-cased.** On RF the sender sits in the "to"
   slot with `group 00 00` in the "from" slot. In a PLM `0x50` broadcast,
   `from` is the sender and `to` is `00.00.<group>`. This must be handled
   explicitly — a blind remap produces a message addressed to nonsense.
3. **Validate before converting.** The RF CRC (and the extended data CRC) is
   dropped, because PLM frames carry none — so it must be checked first, and
   `index_ok` must hold. Never convert a packet whose frame-index counters are
   impossible, even if the CRC matches: that guard is what killed a phantom
   `29.41.E0 -> 5D.99.3C cmd 0x69` decode on live air, and now it guards
   something that gets fed into the real protocol stack.

**Test with upstream's own parser as the oracle**: RF capture →
`to_plm_bytes()` → insteon-mqtt's `Msg.types[code].from_bytes()` → assert the
decoded fields match the `Packet`. Vendor the needed upstream modules into the
test environment so this runs in CI. Round-tripping against the real consumer
is far stronger than asserting against a hand-written expectation.

---

## 6. Deduplication

Five layers, each catching something the others cannot.

### 6.1 On the board — structural
The Manchester gate (§3.4). Discards noise before it reaches MQTT.

### 6.2 Per receiver — hop repeats
The existing `monitor.Deduper`: key = message bytes up to but excluding the
CRC, with the hops-left bits of the flags byte masked. Folds the mesh's own
hop retransmissions.

### 6.3 Across receivers — one fused event
Bucket by that same key:

- First sight of a key opens a bucket with a **600 ms** window.
- Each new copy extends it by 200 ms, capped at **2 s** total.
- On close, emit one fused event carrying the packet, `hops_left` min/max, a
  per-receiver RSSI/copies/crc_ok map, and `closest` (the receiver reporting
  the highest hops-left, tie-broken by RSSI).
- A key reappearing **after** its bucket closed opens a new bucket and is
  emitted with `repeat_index: 1, 2, …`.

That last point is the distinction that must not be lost: a **genuine
retransmission** — an Insteon retry after no ACK, or a second button press —
has hops-left back at maximum and arrives seconds later. It is a real event and
must be injected as such, not folded away. Tune the windows in Phase 5 against
the measured inter-hop gap distribution.

### 6.4 Against the PLM — before injecting
The injector subscribes to `insteon/raw/rx` and keeps its own history keyed the
same way. If the PLM already reported the message, do not inject.

This layer is **not optional**, because insteon-mqtt's window is
`hops_left × 87 ms` — and a copy arriving with `hops_left = 0` has a window of
**zero**. Relying on `_is_duplicate()` alone would double-process exactly the
messages that travelled furthest. Use a fixed window (2 s) here, independent of
hop count.

### 6.5 Inside insteon-mqtt — the backstop
`_is_duplicate()`. Belt and braces for anything §6.4 raced.

### 6.6 Cross-receiver bit combining (needs 3+ receivers)
When a bucket holds copies but **none** has a valid CRC:

1. Align the bit strings on the frame grid (`find_sync` / `parse_bits` already
   establish the offset per capture).
2. Majority-vote per bit position. Thermal noise at different locations is
   independent, so this alone fixes most single-receiver errors.
3. Re-check the CRC; if it passes, mark `corrected: true`.
4. If it still fails, feed the **positions where receivers disagreed** into
   `recover._repair` as the suspect set — a far better-informed suspect list
   than a single receiver's soft confidence produces.

The frame-index guard applies to combined packets too, and must have its own
test: a combined packet with impossible counters is rejected **even when its
CRC passes**. Measure the false-accept rate the way `dsp_bench.py` already does
— over noise bursts and random bit strings — and publish the number before
letting combined packets reach §7.

---

## 7. The injector

Sits in the host-side service, between fusion and `insteon/raw/inject`.

### 7.1 Allowlist, phased in
Start closed and open it deliberately:

1. **Broadcasts from the 26 RF-only battery devices.** Safest and highest
   value: they cannot be polled, so injection is the only mechanism that
   helps, and they are not party to any outstanding command exchange.
2. Group broadcasts and ALL-Link cleanup reports from mains devices.
3. Unsolicited state reports from mains devices.
4. **Never initially: ACK/NAK of an outstanding command.** Injecting a reply
   that insteon-mqtt is actively waiting on interacts with its handler state
   machine and timeouts. Potentially valuable — a command that appeared to
   fail may have been ACKed unheard — but it needs its own evidence and its
   own phase.

### 7.2 Rate limits and controls
- Per-device and global injection rate caps, because §4.2's `set_wait_time`
  side effect means a flood stalls outbound commands.
- A kill switch (HA `input_boolean`) that stops injection without redeploying.
- **Shadow mode**, and it is the default: decode, fuse, decide, log what *would*
  have been injected — publish nothing. Every phase runs in shadow first.
- A counter per device of injected / suppressed / would-have-injected, exposed
  as HA sensors. This is how the project justifies itself.

### 7.3 The measurement that decides everything
Phase 3 exists to answer one question: **how often does the PLM actually miss
something?**

With `insteon/raw/rx` publishing and the dongle listening, that is a week of
data and a straight comparison. Possible outcomes:

- **Misses are common on battery devices, rare on mains** — the expected
  result. Build §7.1 step 1, stop there, and the 21 water sensors get a second
  antenna. Small, valuable, done.
- **Misses are common across the board** — proceed through the allowlist and
  add boards.
- **Misses are rare everywhere** — stop. Keep the raw-topic patch (it is
  useful diagnostics) and keep the all-on watch, and do not build the
  injector. This is a real possible answer and worth accepting cheerfully.

---

## 8. Phases

Each phase has a hard exit criterion. Phases 1–3 need **no new hardware**.

**Phase 1 — RF → PLM conversion.**
`Packet.to_plm_bytes()` (§5) plus the oracle test against upstream's parser.
> **Exit:** every packet in `tests/data/` and every packet captured live by the
> dongle round-trips through insteon-mqtt's own `from_bytes()` with matching
> fields, group broadcasts included.

**Phase 2 — the patch, publish only.**
`Protocol.inject()` present but disabled; `insteon/raw/rx` publishing on.
Custom image, digest-pinned base, patch series (§4.4).
> **Exit:** the patched sidecar runs a week with no behaviour change, and
> `insteon/raw/rx` accounts for every message in `insteon_mqtt.log`.

**Phase 3 — measure the miss rate. The decision point.**
Dongle + fusion in shadow mode, compared against `insteon/raw/rx`.
> **Exit:** a per-device miss table over 7 days. **Decide here whether to
> continue** (§7.3).

**Phase 4 — the Heltec, as receiver #2.**
ESPHome `insteon_rf` external component: radio bring-up, generated sync word,
Manchester gate, RSSI, MQTT publish, OLED, health sensors. First commit is
"sync and dump a FIFO"; first success is a real packet decoding through
`insteon-rf print`.
> **Exit:** 48 h unattended, no reboots, no `seq` gaps beyond WiFi
> reconnects, a stable and explicable accept ratio, and ≥90% agreement with
> the dongle when sitting in the same room.

**Phase 5 — injection, battery devices only.**
Allowlist step 1, shadow for a week, then live with rate limits and kill
switch.
> **Exit:** a deliberately triggered water sensor (a damp finger across the
> probes) reaches HA through the injected path with the PLM's antenna
> disconnected or the device out of PLM range.

**Phase 6 — widen the allowlist.**
Steps 2 and 3, each shadowed first.
> **Exit:** a week live with no duplicate-processing incidents and no measurable
> delay to outbound commands.

**Phase 7 — more boards, fusion, localization.**
Multi-receiver bucketing, cross-receiver combining (§6.6), per-device RSSI
coverage map, and `allon.py` extended to report `closest`.
> **Exit:** every RF-only battery device heard by **at least two** receivers;
> an induced group-0 broadcast attributed to the right node.

---

## 9. Risks

| Risk | Severity | Response |
|---|---|---|
| **Injected message double-processed** | **high** | §6.4 is mandatory — insteon-mqtt's own window is zero at `hops_left = 0`. Plus the allowlist keeps ACK/NAK out of the path, where double-processing would actually hurt. |
| **Combining manufactures a plausible-but-wrong packet** | **high** | Frame-index guard on combined packets, with a test that rejects one whose CRC passes but whose counters are impossible. Publish a measured false-accept rate before Phase 7 feeds combined packets to the injector. |
| Injection throttles outbound commands (§4.2) | medium | Rate limits, and watch command latency during Phase 5/6. |
| SX1262 packet mode cannot capture Insteon framing | medium | Not gating any more, per decision — but the contingency stands: **CC1101 modules** (~$3, same family as the CC1111 dongle, asynchronous serial mode gives a true raw bitstream, register values lift almost directly from `radio/rfcat.py`) on the Heltec's ESP32 as the host. Phases 1–3 are unaffected either way. |
| Fork drifts from upstream | medium | Digest-pinned base, patch series not edited files, and try to upstream the raw topics. |
| False sync storm with the preamble detector off | medium | The Manchester gate; then a longer sync word, a higher RSSI floor, or a stricter gate. |
| TCXO misconfigured on the V3 (DIO3) | medium | A radio that never syncs and never errors is this bug. Verify against the Heltec V3 schematic — V2 and V3 differ. |
| The PLM turns out to miss almost nothing | — | Then Phase 3 says so and we stop, having spent no money and gained a diagnostic topic. That is a good outcome, not a failure. |

---

## 10. Open questions

1. **Where does the first board go?** Driven by Phase 3's miss map, but also by
   where the 21 water sensors physically are — that list is worth writing down.
2. **How to induce a group-0 broadcast safely** for the Phase 7 exit test.
   Every legacy device in the house will turn on, so it wants to be a
   deliberate choice at a convenient hour rather than a surprise.
3. **Upstream the raw topics?** Worth asking TD22057 before carrying a patch
   indefinitely.
