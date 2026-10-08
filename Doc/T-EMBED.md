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
| FOCCFG / BSCFG | `0x17` / `0x6E` | same |
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

## Tuning

- **Frequency.** The `Frequency Offset` sensor is the CC1101's FREQEST
  averaged over each minute's accepted packets. Insteon devices disagree
  among themselves by a few kHz (the V3 saw a 9 kHz spread), so look at the
  mean over an hour. If it sits well away from zero, move `frequency:` by
  that amount, in the same direction as the sign, and it should then read
  near zero. FOC (±BW/2) absorbs the rest.
- **Next to the PLM.** If captures arrive clipped or the gate rejects loud
  packets, use `rx_attenuation: 12dB` or `max_lna_gain_reduction`. The V4's
  lesson was that placement beats any amount of tuning.
- **Sync tolerance.** `sync_mode: 15/16 + carrier` accepts one bit error in
  the start header: more weak packets, more noise syncs, and the gate takes
  the noise. A/B it against the dongle the way the Heltec settings were
  (CLAUDE.md, "Heltec A/B record") before keeping it.
- `capture_bytes` stays 162; the reasoning is in `__init__.py`, and
  `tests/test_listener_firmware.py` pins it to the slot grid for both sync
  lengths.

## Display

Two pages; turn the knob to switch, press it to toggle the backlight.

- **Survey**: RSSI of the last accepted packet (big, with a bar from -110 to
  -50 dBm), captures and accepts per minute, lost per minute if any, the age
  of the last packet and the lifetime count. The same read-outs as the
  Heltec's OLED, for walking the board around on battery.
- **Health**: carrier offset, losses, battery %/V, WiFi RSSI, uptime and the
  firmware version.
