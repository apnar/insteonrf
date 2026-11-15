# Re-architecture: decode at the listener, decide in insteon-mqtt

Status: **implemented 2026-11-15**, going straight to the end state (no
parallel phases, no separate raw-topics PR). Supersedes the injection half of
`MESH-PLAN.md`; the `insteonrf-mesh` pod is parked in `/k8s/yaml/limbo/`.
Where the build differs from the plan below, **As built** says so.

## As built

- insteon-mqtt: branch `rf-listeners` in `/root/src/insteon-mqtt` (one PR, not
  two). `insteon_mqtt/Listeners.py` holds the engine *and* its MQTT side
  (subscribe, stats, alerts); there is no `Raw.py` and no `insteon/raw/*`
  topics. Config is `mqtt.listeners` with `inject:` (live tiers) and
  `shadow:` (logged-only tiers) lists; the section's presence enables it.
  Releases run from one `modem.timed_call` tick (TimedCall runs a single call
  per loop pass, so one per event would trickle a burst out).
- Alpha runs `inject: ['battery']`, `shadow: ['group', 'state']`.
- Rule 1 does **not** require the extended data CRC (the packet CRC covers
  every data byte, and some devices leave it unset).
- The T-Embed does **no** repair (no bitwise majority across copies):
  `repaired` is always 0 from the board. Firmware 3.0.0.
- Host pods: `monitor --publish-decoded=<rx>` (not a new `listen`
  command). Both publish `rssi: null` (the dongle's register snapshot is the
  post-block floor; the SDR measures none).
- After four review passes and a Fable review with measurements (2026-11-15),
  insteon-mqtt's decision model is: same bytes within 0.5 s on air = one
  transmission; each modem read credited once, plus any modem read of the same
  bytes within 2 s on air = heard (device retransmissions); other form
  (broadcast vs cleanup) within 8 s = paired; a newer same-state message the
  modem read = superseded (leak groups 1/2 are one state); cleanup reports never
  injected. Windows: GRACE 2.5, EARLY 2.5, MATCH 2.0, ORDER 5, PAIR 8, STALE 10.
  Listener fold window 0.35 s. Measured: listener publish lag ~0.75 s, PLM lag
  p50 0.2 / p99 0.93 s, cross-listener on-air spread p99 0.46 s; the PLM misses
  ~1 battery *state* message per 4 days (most battery misses are 0x06 reports).
- Final review (2026-11-15): shadow decisions have their own rate-limit
  budget; after an injection, `Protocol.read_filter` (set by Listeners) drops
  the modem's own late copy of the same bytes within 1 s, or of the other form
  (broadcast vs cleanup) within 8 s, once. This replaces the 10 s guard in
  `inject()` that the plan below describes. Retransmission and pairing windows
  are measured on insteon-mqtt's clock (arrival - 0.75 s listener lag), never
  the listener's.
- The dongle keeps `--watch-all-on` and the HA automation on
  `insteon-rf/alert`; insteon-mqtt's `insteon/listeners/alert` (group 0 from
  any listener) runs alongside it.


## Why

Today every listener publishes raw captures (`insteon-rf/rx/<rx>`), and one
service (`insteonrf-mesh`) decodes, fuses, compares against the PLM and would
inject. For anyone else to use this they would need a fourth component, three
hand-made files (`known.txt`, `battery.txt`, `--plm-addr`) and a container,
and the injection path has never run live.

The fusion step barely earns its place. From the mesh log, 2026-11-04 to
11-15: 20,363 valid device messages, 4,547 of them missed by the PLM (22%).
Cross-receiver bit combining rescued **8** (0.04%; 6 PLM misses). Repair
touched 145, and a repaired message is never injected anyway. Each receiver
decoding on its own loses essentially nothing.

## Target

```
 T-Embed (ESPHome)        decodes on device ──────┐
 insteonrf-dongle pod     rfcat, hard decode ─────┤  insteon-rf/msg/<rx>   (contract v1, below)
 insteonrf-v3 pod         RTL-SDR, soft decode ───┤
                                                  ▼
                insteon-mqtt  mqtt.raw.listeners  ── correlate · PLM check · tier · rate limit
                     │                                └─> Protocol.inject()   (shadow by default)
                     └─> insteon/listeners/stats (retained), insteon/listeners/alert
```

- Listeners know nothing about the network. They publish every valid message
  they hear, once per transmission.
- insteon-mqtt makes every decision. It already knows the PLM address, every
  device, which devices are battery (`isinstance(dev, BatterySensor)`) and
  what the PLM heard (in-process, `Protocol.signal_raw_read`). The three
  hand-made files go away.
- `insteon/raw/rx` and `insteon/raw/inject` (PR 1) stay as optional external
  interfaces; the in-process path uses neither.

## Contract v1: decoded listener message

Topic `insteon-rf/msg/<rx>`, QoS 0, not retained. A new topic, not the old
`insteon-rf/rx/<rx>`, so both can run side by side during the migration.

```json
{"v":1,"rx":"insteon-t-embed","t":1794759872.429,
 "frame":"0250294E522B9307270D02","copies":3,"hops_left":2,
 "rssi":-78.5,"snr":null,"repaired":0}
```

| Field | Meaning |
|---|---|
| `v` | Contract version. insteon-mqtt ignores versions it does not know. |
| `rx` | Receiver name (also the last topic segment). |
| `t` | Epoch seconds of the **earliest copy's first bit on the air**. |
| `frame` | The message as a PLM inbound frame, hex: `0250…` (11 bytes) or `0251…` (25). Same bytes the PLM would have produced, so insteon-mqtt parses it with its own `Msg` classes. Flags carry the earliest copy's hops. |
| `copies` | Hop copies this receiver heard of this transmission. |
| `hops_left` | Highest hops-left seen, i.e. the copy closest to the original. |
| `rssi` | dBm of the strongest copy (`null` if the receiver has none). |
| `snr` | Symbol SNR in dB (SDR only, else `null`). |
| `repaired` | Bits changed by repair: 0 = clean. Non-zero is counted in stats and **never injected**. |

Rules every listener follows:

1. Publish only messages with a valid packet CRC **and** valid frame-index
   counters (the extended data CRC is not required; see As built). The counters are what keep
   an 8-bit CRC from accepting one random string in 256.
2. One message per transmission: fold hop copies by the hop-insensitive key
   (flags minus hop bits, to, from, cmd1, cmd2, extended data) within 0.35 s (0.5 s before 2026-11-15)
   of on-air time. A device retry later than that is a new message.
3. Publish the PLM's own transmissions too. insteon-mqtt drops them; they are
   useful for coverage and clock checks.
4. Group broadcasts swap the RF address slots; `frame` uses the PLM's
   layout. The reference is `insteonrf.plm.to_plm_bytes`.

Test vectors: `tests/data/contract-v1/` holds capture → expected messages,
generated from the Python decoder. The ESPHome decoder, the containers and
insteon-mqtt's parser are all tested against the same files.

## insteon-mqtt: `mqtt.raw.listeners`

PR 2, stacked on PR 1 (`rf-listener-raw-topics`).

Config:

```yaml
mqtt:
  raw:
    listeners:
      enable: true
      topic: 'insteon-rf/msg/+'
      inject: [battery]        # tiers: battery, group, state; [] = observe only
      shadow: true             # decide and log, inject nothing (default)
      stats_topic: 'insteon/listeners/stats'
      stats_interval: 300
```

Files (keeps the PR small):

| File | Role |
|---|---|
| `insteon_mqtt/Listeners.py` (new) | The decision engine, no MQTT: correlate, PLM memory, classify, rate limit, stats. Time is passed in, so it is unit-testable. |
| `insteon_mqtt/mqtt/Raw.py` | Subscribes `topic`, parses contract v1, feeds the engine, publishes stats and alerts. |
| `insteon_mqtt/Protocol.py` | `inject()` remembers what it injected for 10 s, so the PLM's own late copy is dropped (below). |
| `data/config-schema.yaml`, `config-example.yaml` | The `listeners:` section. |
| `tests/test_Listeners.py` (new), `tests/mqtt/test_Raw.py` | Engine against contract-v1 vectors and timelines; wiring. |

The engine, per message key:

1. **First sighting** opens an event and schedules a release with
   `modem.timed_call` at `t + 2.5 s`. Later sightings from other receivers
   join it (`heard_by`). The PLM's copy travels the powerline as well and was
   measured landing 0.7-1.3 s after the first RF sighting, hence the grace.
2. **PLM memory.** Every 0x50/0x51 frame from `signal_raw_read` is
   remembered by key for 30 s. At release the PLM "heard it" if a matching
   frame falls within `[t - 0.5 s, t + 2.5 s]`, two-sided because either
   copy can come first.
3. **Classify**, if the PLM did not hear it:

   | Tier | Message |
   |---|---|
   | `battery` | Broadcast or cleanup report from a `BatterySensor` device |
   | `group` | Any group broadcast or cleanup report from a known device |
   | `state` | Unsolicited direct message to the modem |

   Never injected: ACK/NAK, anything from the modem's address, senders not in
   `modem.devices`, `repaired > 0`, and **group 0** (phantom all-on: an alert,
   never a state change).
4. **Rate limit**: carried over from `insteonrf/inject.py`, 30 s per device and
   6 per minute overall. The 30 s per device is likely too tight for a leak
   sensor that goes wet then dry; revisit with real data in Phase 4.
5. **Inject**: `Protocol.inject(frame)` (shadow: log the decision only).

The **late-copy guard** in `Protocol.inject()` covers the PLM's copy arriving
after the grace. insteon-mqtt's own duplicate window is `hops_left × 87 ms`
(zero at 0 hops left), so without the guard the late copy would be handled a
second time.

**Stats** (log and retained JSON every `stats_interval`): per receiver, heard
and heard-alone; per device, messages, PLM-missed, injected, suppressed. This
replaces the mesh's miss table. HA discovery sensors can follow later.

**Alerts** on `insteon/listeners/alert`: group 0, or a group the modem has no
link for, from any listener. The modem knows its own groups, so no
`known-groups` file either.

## T-Embed: decode on the device

The work is in the `insteon_rf` ESPHome component (this repo), and the
`insteon-t-embed` repo moves its pin.

- New `decoder.cpp/.h` with no ESPHome dependencies: start-header search in
  both polarities, Manchester decode, 28-bit frame parse, index check,
  standard and extended CRC, PLM-layout conversion. It is a port of
  `packet.parse_bits` and `plm.to_plm_bytes`.
- A 162-byte capture holds up to three hop slots. Decode each, fold copies
  (rule 2), and when one copy fails its CRC, take a bitwise majority across
  the copies that decoded (`repaired` counts the flipped bits). No
  CRC-search repair on the board.
- Keep a 0.35 s key window across consecutive captures, so a transmission that
  spans two captures is published once.
- New option `publish: decoded | captures | both` (default `decoded`).
  `both` is for the migration and for A/B work.
- The host-side harness compiles `decoder.cpp` with `g++` into a small CLI,
  and pytest runs it over `tests/data` captures and contract-v1 vectors
  against the Python decoder. It runs in `make check`.
- Version 3.0.0, since the contract changed.

## Containers: dongle and V3

Same image (`localhost:32000/insteonrf:<date>`), two pods as today, each
decoding its own radio:

- New `insteon-rf listen` command (built from `monitor`'s receive path and
  `MqttPublisher`): decode, fold copies, publish contract v1, with
  `--publish-captures` for the migration.
- **`insteonrf-dongle`**, rfcat, hard bits: decode, then bounded one-flip CRC
  repair guarded by the frame counters (`recover`), published as
  `repaired: 1`. Keeps `--watch-all-on` until insteon-mqtt's alert has
  proven itself (Phase 5).
- **`insteonrf-v3`**, RTL-SDR, soft decisions: the existing `--demod numpy` ML
  chain and soft framing. Soft repair is where it pays (Manchester pairs
  combined by difference, least-confident bits tried first); `snr` is filled
  in.

They stay two pods on one image because the decoder is shared code and the
two radios need different hardware access; a split image buys nothing.

## Phases

Every phase runs **next to** the current system; nothing is removed until
Phase 5.

| # | Work | Done when |
|---|---|---|
| 0 | Contract v1 doc and vectors; record the baseline week (coverage per receiver, PLM-missed per device, leak heartbeats reaching HA per sensor per day) | Vectors in `tests/data/contract-v1/`; baseline JSON saved |
| 1 | `insteon-rf listen` in both pods with `--publish-captures`, so the mesh keeps working | 48 h: each pod's decoded count per device matches the mesh's per-receiver heard count within 1% |
| 2 | T-Embed decoder, host harness, firmware 3.0.0 with `publish: both` | Harness green; 48 h on air matching the mesh within 1% |
| 3 | insteon-mqtt PR 2 on alpha, `shadow: true`, `inject: [battery, group, state]`; the old mesh runs `--inject=battery` shadowed in parallel | 1 week: both make the same battery decisions; no PLM transmission or unknown sender is ever "would-inject" |
| 4 | `inject: [battery]`, `shadow: false` | 1 week: every leak sensor's heartbeat reaches HA daily (baseline: the PLM misses most); no duplicate HA state changes; then `group`, then `state`, a week of shadow each |
| 5 | Turn off captures (`publish: decoded`, no `--publish-captures`); retire `insteonrf-mesh` or keep it observe-only on `insteon-rf/msg/+` for `score_receivers.py`; move the HA all-on automation to `insteon/listeners/alert`; update `docs/insteon.md`, `T-EMBED.md`, the insteon-t-embed docs (no mesh to run any more) | Docs merged; mesh pod parked in `limbo/` |
| 6 | Upstream PR 2 | Opened against `dev` |

Rough effort: Phase 1 about a day, Phase 2 two to three days, Phase 3 two to
three days. Phases 3 and 4 then take calendar time, about a month of
watching.

## Rollback

- Listeners: `publish: both` / `--publish-captures` keep the old path fed
  throughout.
- insteon-mqtt: `listeners.enable: false`, or `shadow: true`, then a sidecar
  restart. The image tag rolls back with `kubectl set image`.
- Firmware: OTA back to 2.10.0.

## Open decisions

1. **PR 1 now or later.** Submit the raw-topics PR as it is and stack PR 2 on
   it, or hold it and submit one "RF listener support" PR after Phase 4?
2. **Topic names.** `insteon-rf/msg/<rx>` for decoded messages and
   `insteon/listeners/*` for insteon-mqtt's output.
3. **The mesh after Phase 5**: retire it, or keep it observe-only for A/B
   scoring and history.
4. **All-on detection**: move it into insteon-mqtt (it sees every listener and
   knows the modem's groups) or keep it in the dongle pod.
