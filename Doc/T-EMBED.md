# The T-Embed CC1101 listener

A LilyGO T-Embed CC1101 (or CC1101 Plus) as a fourth receiver in the mesh,
next to the rfcat dongle, the Heltec and the V3. Receive only, like every
other board: there is exactly one transmitter on an Insteon network, and it
is the PLM.

Firmware: `esphome/insteon-rf-embed.yaml` + the `insteon_rf` component with
`radio: cc1101`. It publishes to `insteon-rf/rx/insteon-rf-embed` in the same
format as the Heltec, so `insteon-rf mesh` picks it up with no change: it
subscribes to `insteon-rf/rx/+`, and the receiver name comes from the
payload.

## Why this radio is a good bet

The CC1101 is the radio half of the CC1111, the chip in the rfcat dongle,
and the dongle is the best receiver on this network. So the modem
configuration is not a guess: it is the dongle's own registers, read from
the `insteonrf` pod's `radio at start` dump on 2026-10-08 and rescaled from
the CC1111's 24 MHz crystal to the CC1101's 26 MHz.

| | dongle (CC1111, 24 MHz) | T-Embed (CC1101, 26 MHz) |
|---|---|---|
| sync | `0x3155`, 16/16 + carrier sense | same |
| data rate | 9132.4 baud | 9125.0 baud |
| deviation | 76.17 kHz | 76.17 kHz |
| channel filter | 187.5 kHz | 203 kHz (nearest step) |
| FOCCFG / BSCFG | `0x17` / `0x6E` | **`0x37`** (loops gated on carrier sense, measured below) / same |
| AGCCTRL2/1/0 | `03 40 91` | same |
| TEST2/1/0 | `81 35 09` | same |
| PQT, CRC, whitening, Manchester | off | off |
| tuned to | 914.950 MHz (its crystal is off) | 914.990 MHz (where the network is) |

The sync word, the bit polarity and the gate layout are therefore the same
as the dongle's and the Heltec's: the 16-bit word ends exactly where the
Heltec's 32-bit one does, at the end of the start header, so the FIFO starts
on the same bit and the Manchester gate is shared code.

## What is new in the driver

The CC1101's RX FIFO is 64 bytes, which is 56 ms of air; a capture is 162
bytes (three slots, 142 ms). So the FIFO is drained *while* the packet
arrives. ESPHome's own `cc1101` component reads a packet only after it ends,
which caps it at 64 bytes; hence a driver of our own.

- **A FreeRTOS task drains the FIFO**, pinned to core 1 at priority 5 (above
  `loopTask`, below WiFi and lwIP). The display, SD slot, nRF24 and CC1101
  share one SPI bus, and a display refresh or an MQTT reconnect that held
  `loop()` up past 56 ms would overflow the FIFO. That is the dongle's own
  failure mode (its firmware drops a packet the host has not shipped yet), so
  the cure is the pod's: nothing heavy on the receive thread. The task only
  moves bytes and stamps them; the gate, the RSSI floor and the MQTT publish
  run in `loop()`, fed through a queue of 8 captures.
- **GDO2 = RX FIFO at or above 8 bytes, GDO0 = sync to end of packet**, so
  "anything to read?" is a GPIO read every millisecond. A 20 ms SPI poll
  backs them up in case an edge is missed; without GDO pins it polls SPI
  every 2 ms.
- **The FIFO is never read empty mid-packet** (errata SWRZ020), and RXBYTES
  is read until two reads agree.
- **The timestamp comes from the byte count.** When a capture's first byte
  is seen, `avail` bytes sit in the FIFO and the newest has just arrived, so
  the first bit was on the air `avail × 877 µs` ago. That is good to within
  the byte in the shift register (< 0.9 ms) with no interrupt handler.
- **RSSI and FREQEST are sampled at the first drain**, while the synced
  packet is still on the air, so they are that packet's and not whatever
  comes next. (The Heltec learned that one the hard way; see `sx1262.cpp`.)
- **Recovery:** an overflow, a stalled capture (no progress for 20 ms), or a
  radio found outside RX on two health checks a second apart is re-armed
  with SIDLE/SFRX/SRX. The synthesiser is recalibrated between packets
  every 15 minutes. Each lost capture is counted in the `Lost` sensor.
- **Fixed-length capture with RXOFF = stay in RX:** the next slot is hunted
  for the moment a capture completes, exactly like the Heltec's continuous RX.

## Board notes

Pins (LilyGO `examples/utilities.h`, `docs/pinmap_cn.md`):

| What | GPIO |
|---|---|
| SPI SCK / MOSI / MISO (shared) | 11 / 9 / 10 |
| CC1101 CS / GDO0 / GDO2 | 12 / 3 / 38 |
| RF band switch SW1 / SW0 | 47 / 48 (SW1=0, SW0=1 = 868/915 MHz) |
| BOARD_PWR_EN (CC1101 + LED supply) | 15 |
| LCD CS / DC / backlight | 41 / 16 / 21 |
| SD CS | 13 |
| nRF24 CS / CE (Plus) | 44 / 43 (= UART0 RX/TX) |
| I2C SDA / SCL | 8 / 18 |
| encoder A / B / press | 4 / 5 / 0 |

Traps, each of which produces a radio that hears nothing and reports no
error:

- **PWR_EN low**: the CC1101 is unpowered. The driver checks PARTNUM and
  VERSION and writes and reads back the sync word, so this is reported at
  boot as `CC1101 not responding`.
- **The band switch** left on the 315 or 433 MHz filter. The driver sets
  SW0/SW1 from `frequency:` itself rather than trusting a YAML switch.
- **A floating chip select** on the SD card or the nRF24 lets that chip
  drive MISO during a radio read. The YAML holds 13 and 44 high and the
  nRF24's CE low. Because 43/44 are UART0, the logger is pinned to
  USB-Serial-JTAG.

The PN532 is held in power-down (GPIO45 low). The BQ25896's charge current
is capped in hardware by its ILIM resistor, so nothing is written to the
charger; the BQ27220 gauge is read for the Battery sensors.

## Flashing

The first flash has to be over USB; after that it is OTA like the other
boards.

1. Build the factory image in the esphome sidecar (`navien.md` has the `$E`
   environment line):
   ```bash
   microk8s kubectl exec homeassistant -c esphome -- $E esphome compile /config/insteon-rf-embed.yaml
   # -> /config/.esphome/build/insteon-rf-embed/build/firmware.factory.bin
   ```
   or from the dashboard: insteon-rf-embed → Install → Manual download →
   Factory format.
2. Put the board in download mode: hold the encoder button (GPIO0 = BOOT),
   tap the reset button on the side, release the encoder. LilyGO's stock
   firmware does not expose ESPHome's serial protocol, so this is needed
   once.
3. Flash from Chrome at https://web.esphome.io (Connect → Install → pick the
   factory .bin), or plug the board into alpha and run
   `esptool.py --chip esp32s3 -p /dev/ttyACM1 write_flash 0x0 firmware.factory.bin`.
   It enumerates as Espressif `303a:1001`; `303a:4001` on this host is the
   Z-Wave stick, not the T-Embed.
4. Reset. After that: `esphome upload /config/insteon-rf-embed.yaml --device
   insteon-rf-embed.local` for OTA.

## Bring-up

Watch `esphome logs /config/insteon-rf-embed.yaml` (or the dashboard). The
first ten captures are logged at INFO:

```
capture 1/10: 162 bytes at -78.5 dBm, offset +3.2 kHz, gate PASS, starts ...
```

- `CC1101 not responding (PARTNUM .., VERSION ..)`: PWR_EN or SPI.
- Nothing at all while the dongle hears traffic: check the band-switch
  pins in `dump_config`, then try `sync_mode: 16/16` (carrier sense off).
- Captures with `gate fail` on every one: framing. Send the logged bytes.
- `RX FIFO overflow` warnings: the radio task is being starved. Raise the
  display's `update_interval` or lower its `data_rate` first.

Then confirm the board in the mesh. `insteon-rf mesh`'s miss report ends with
receiver coverage, which scores every receiver against the same set of
device messages; `insteon-rf-embed` should appear there within an hour of
normal traffic. Score it against a *fixed* receiver set (see CLAUDE.md):
"% of all fused events" moves whenever any receiver drops out.

## Tuning results (2026-10-08)

Three rounds of five-minute A/Bs under identical traffic: Get Engine Version
every 1.5 s, round-robin to seven devices around the house (`29.4E.52`,
`2B.A0.AB`, `38.FA.56`, `40.CE.68`, `3B.8F.8A`, `25.08.BC`, `29.4D.F8`).
Scored with `tools/score_receivers.py --ref v3`: of the messages the V3
decoded, how many the T-Embed decoded too. The dongle could not be part of
the reference, because it wedged on USB at 13:10 that day. Settings were
changed live (see "Live tuning" below), with a baseline repeated between
groups.

| setting | device replies | PLM | PLM first copy |
|---|---|---|---|
| baseline (FOCCFG 0x17), 7 runs | 93.0-98.4% | 98.1-99.5% | 55-62% |
| **FOCCFG 0x37** (loops frozen until carrier sense), 4 runs | 97.9-99.5% | 99.0-100% | **87-94%** |
| 0x37 + carrier sense -4 dB | 98.9% | 98.0% | 58% |
| 0x37 + carrier sense +3 dB | 96.9% | 98.6% | 90% |
| 0x37 + 15/16 sync | 97.2% | 99.5% | 93% |
| 0x36 (gate, FOC limit BW/4) | 98.4% | 100% | 91% |
| 0x35 (gate, FOC limit BW/8) | 97.4% | 99.5% | 85% |
| 0x15 (no gate, FOC limit BW/8) | 96.8% | 99.5% | 53% |
| sync 15/16 + carrier | 95.2% | 99.0% | 67% |
| sync 16/16, no carrier sense | 98.9% | 98.6% | 58% |
| carrier sense -4 / +3 dB | 96.3 / 96.1% | 99.0 / 99.5% | 56 / 59% |
| channel filter 232 kHz | 94.6% | 95.6% | 61% |
| channel filter 270 kHz | 26.4% | 12.9% | 8% |
| channel filter 162 kHz | 77.6% | 83.2% | 51% |
| AGCCTRL0 0x80 (faster AGC) | 96.8% | 98.5% | 56% |
| AGCCTRL2 0x43 (lower AGC ceiling) | 98.5% | 98.6% | 57% |
| IF 203 kHz (FSCTRL1 0x08) | 97.9% | 99.0% | 50% |

"PLM first copy" is how often the T-Embed heard the PLM's *original*
transmission, not just a hop repeat. That copy arrives out of silence,
and it is the one that separated the settings: the CC11xx radios lost it
about 40% of the time (the dongle about 37%), and caught the repeat 50 ms
later instead. The cause was the frequency-offset and bit-sync loops.
Free-running, they track noise between bursts; the noise captures showed
offsets of -11 to -57 kHz. A burst out of silence then starts with both
loops mis-set. Gating them on carrier sense (FOCCFG bit 5) fixes it. The
gate is only as good as carrier sense, though: at -4 dB noise holds it open
and the gain vanishes. So the threshold stays at 0.

Everything else was within the baseline's own scatter, or worse. 203 kHz is
the right filter: 162 kHz clips a ±76 kHz signal, and 270 kHz collapses
with this IF.

**Applied in 2.9.1:** FOCCFG `0x37` by default. Everything else is unchanged:
16/16 + carrier, 203 kHz, carrier sense 0, 914.990 MHz (the T-Embed's
FREQEST and the V3 both put the devices within a few kHz of it).

**The dongle had the same weakness and has the same fix** (2.9.1,
`RfcatRadio.configure_rx()` writes FOCCFG 0x37; `--foccfg 0x17` restores
the old value). Interleaved on the live pod the same evening, scored against
the V3 under the same traffic:

| dongle FOCCFG | PLM first copy | device replies |
|---|---|---|
| 0x17, 3 windows | 61.2-63.7% | 92.1-95.2% |
| 0x37, 4 windows (before and after the 0x17 control) | 91.1-98.5% | 93.2-97.9% |

## After silence and back-to-back (2026-10-09)

The 2.9.1 tuning ran with an exchange every ~1.2 s, and that hid two cases.
Over one night of natural traffic (17:00-09:00), scored against the V3:

- **A PLM message after >= 2 s of quiet** was caught first-copy only ~42% of
  the time by the T-Embed and ~56% by the dongle. The V3 caught 100%.
- **A PLM message within 0.5 s of other traffic** was caught ~75% of the time
  by the T-Embed and ~88% by the dongle.

**The PLM's signal is not the cause.** Bucketed by the preceding quiet, the
V3 measures the same carrier offset (-7.3 kHz) and SNR (33 dB) for every
gap. A raw I/Q recording on the spare SDR shows a steady envelope, a constant
offset and an on-grid start for the originals. The weakness is in the CC11xx
receiver.

Measured with `tools/test_traffic.py --pause 8` (bursts out of silence),
10-minute windows, against the V3, "1st" for PLM messages after >= 2 s of
quiet:

| T-Embed setting | after-silence 1st | dongle in the same window (unchanged) |
|---|---|---|
| baseline, 4 runs | 42-52% | 46-67% |
| FOC off (FOCCFG 0x34) | 53% | 55% |
| FOC gentler/limited (0x25) | 53% | 47% |
| relative carrier sense +6 dB, 3 runs | 61-73% | 41-51% |
| relative +10 dB, absolute off | 69% (but 89% decoded) | 49% |
| absolute carrier sense +3 dB | 45% | 58% |
| **re-arm RX after 500 ms idle** | **95%** | (dongle down) |
| **re-arm RX after 2 s idle** | **93%** | |
| relative +6 dB *and* re-arm 500 ms | 66% | |

**Something in the CC1101's RX state goes stale after a second or two without
a packet, and re-entering RX clears it.** The frequency-offset loop is not
it, since switching the loop off entirely changed nothing. 2.9.2 re-arms
after 1 s of quiet air (`idle_rearm`). A re-arm costs ~0.8 ms of deafness
(it recalibrates), once a second while nothing is arriving. Stock 2.9.2,
confirmed: 97% and 93% after silence, and 95% for 1.5 s spacing (no
regression).

**Back-to-back:** a capture-geometry attribution over 90 minutes of the
T-Embed's own captures put most rapid-burst misses inside or at the end of a
capture that had outlived its exchange (21 of 36). The rest were plain
sync misses (15). The idle re-arm alone raised busy first copies to ~92%.
**`quiet_end`** (end a capture after 20 ms without carrier sense; packets in
one exchange are 10 ms apart) adds a little on top. Three interleaved pairs
under closed-loop back-to-back traffic: first copy 91.6% -> 93.3% and
decoded 99.0% -> 99.8%, with device replies unchanged at ~97%.

## Live tuning

Number/select entities (entity category *config*), applied by the radio
task between captures and **not** restored across a reboot, so a reboot
always returns to the YAML:

- `Tune Frequency Offset` (kHz from 914.990), `Tune Bandwidth`,
  `Tune Sync Mode`, `Tune Sync Word` (3155/CEAA), `Tune Carrier Sense
  Threshold`, `Tune LNA Gain Reduction`, `Tune RX Attenuation`,
  `Tune Band Switch` (auto/315/433/915).
- `Tune Idle Rearm` (ms, 0 = off) and `Tune Quiet End` (ms, 0 = off).
- `Tune Register`: value = address × 256 + byte, e.g. 6455 = `0x1937` writes
  FOCCFG = 0x37; 65535 clears all overrides. Any config register, on top
  of the computed configuration.
- Buttons `Scan Wide` (860-960 MHz) and `Scan Narrow` (914.5-915.5 MHz):
  the peak RSSI per step, logged at INFO. Captures stop while a scan runs.
  Generate traffic during it.

The board was not adopted in Home Assistant when these were run. The same
entities are reachable over the native API, with
`tools/embed_api.py`, copied to `/k8s/homeassistant/esphome/.tune/` (it runs in the esphome sidecar):

```bash
kubectl exec homeassistant -c esphome -- python3 /config/.tune/embed_api.py \
    192.168.88.156 insteon_rf_embed_tune_register=6455 --watch 120
```

Other knobs, when placement changes:

- **Frequency.** The `Frequency Offset` sensor is the CC1101's FREQEST,
  averaged over each minute's accepted packets. If it sits well away from
  zero for an hour, move `frequency:` by that amount.
- **Next to the PLM.** If the gate rejects loud packets, try
  `rx_attenuation: 12dB`.

## Diagnostics

Every minute the board logs a status line at INFO:

```
radio: MARCSTATE 0x0D, RSSI -106.5 dBm (peak -40.5), RXBYTES 0x00; last 60s: 88 syncs, 88 captures,
651 accepted total; overflows 0, stalls 0, re-arms 0, reprogrammed 0, deaf resets 0
```

On noise alone, a 16-bit sync matches about 8 times a minute
(9124 bit/s ÷ 2^16); every one of those captures fails the gate.
**Syncs at about that rate, with nothing accepted while the other receivers
hear traffic, means real packets are not syncing.** That was the state of
the first ~20 minutes after flashing on 2026-10-08. It was never
reproduced: the same firmware, reflashed, received normally. So the
firmware now resets the chip outright after five minutes of strong signal
(peak ≥ -80 dBm) with nothing passing the gate, and reads its registers back
every minute.

## Display

Two pages; turn the knob to switch, press it to toggle the backlight.

- **Survey**: RSSI of the last accepted packet (big, with a bar from -110 to
  -50 dBm), captures and accepts per minute, lost per minute if any, the age
  of the last packet and the lifetime count. The same read-outs as the
  Heltec's OLED, for walking the board around on battery.
- **Health**: carrier offset, losses, battery %/V, WiFi RSSI, uptime and the
  firmware version.
