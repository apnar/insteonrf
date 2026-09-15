# Can the listener mesh repeat for the PLM?

Question: use the ESPHome boards as extra repeaters, so messages the PLM
sends are heard more reliably.

**Short answer: no for simulcast repeating, and for the direction that
actually fails the mesh already has a better answer that needs no
transmitter. There is one narrow niche where transmitting would help, and two
cheaper things to try first.**

Measured on this network 2026-09-15 with the rfcat dongle; 14 captures of
`get_engine` exchanges across six dual-band devices.

---

## 1. What the measurement shows

Each capture is a 257-byte block = 225 ms of air, so a whole repeat chain
fits in one block and the bit offsets give timing to ±110 µs (one symbol).

**Repeats sit on a rigid slot grid.** Start-to-start pitch between
consecutive copies, over 31 measured intervals:

| Pitch | In bits | Count | Interpretation |
|---|---|---|---|
| **49.98 ms** | 456 | 25 | one slot |
| 99.85 ms | 911 | 3 | two slots (an empty slot between) |
| 149.8 ms | 1367 | 1 | three slots |
| off-grid | — | 2 | false syncs (1–2 byte "packets") |

A standard packet is 13 frames × 28 bits = 364 bits = 39.89 ms, so a slot is
364 bits of packet plus ~92 bits (10 ms) of dead time. **The pitch does not
vary with packet length** — 11-byte and 13-byte decodes both sit 456 bits
apart — which is the definition of a slot rather than a turnaround delay.

49.98 ms is **six half-cycles of 60 Hz** (6 × 8.333 = 50.0 ms). Insteon
powerline messaging is locked to the AC zero crossing, and the RF side is
slotted on the same clock.

**One transmission per slot, always.** Across every capture, no slot ever
contained two overlapping or offset copies. Where a capture holds two copies
at the same `hops_left` (8 of 14), they are always a whole number of slots
apart — two separate floods in one 225 ms block (a command and its ACK, or a
retry), never two repeaters colliding inside one slot.

That is the signature of **synchronous simulcast**: every repeater in earshot
transmits the same hop at the same instant, and the receiver decodes one
clean packet. If repeaters fired independently, a receiver with several in
range would see overlap and garbage.

**`max_hops` is already being tuned.** Observed `mh:1`, `mh:2` and `mh:3` on
different devices, with insteon-mqtt logging `MsgHistory: Average hops 2.0,
using 2` — it lowers the hop budget for good links and raises it for bad
ones.

---

## 2. Why simulcast from a Heltec board will not work

To join a slot you must be bit-aligned *and* frequency-aligned with the real
repeaters already in it.

**Frequency offset is the blocker.** Simulcast FSK needs the transmitters'
carriers far closer together than the deviation. Insteon's deviation is
75 kHz. Two transmitters even 20 kHz apart produce a beat note comparable to
the modulation itself, which puts deep nulls in the coverage pattern and
mangles the discriminator output. The Heltec's TCXO is good (±2 ppm ≈ ±1.8 kHz
at 915 MHz), but the devices being simulcast with are crystal-based and can
sit tens of kHz off. Joining a simulcast you cannot frequency-lock to
**degrades the slot you were trying to reinforce** — it makes marginal links
worse, which is precisely the population you were trying to help.

**Timing is achievable but tight.** The slot grid can be derived from the
received packet (the next slot starts 456 bits after the last one started),
so no AC reference is needed. But an ESP32 would have to hit that to a
fraction of a 110 µs symbol, through SPI command latency and SX1262 TX ramp.
Hard, and pointless while the frequency problem stands.

**More repeaters do not buy reach anyway.** The limit is `max_hops` ≤ 3, not
the number of participants. Every repeater in earshot already joins every
slot, so the flood is already maximal within its budget. An extra transmitter
only adds reach where *no* existing repeater hears the message at all.

---

## 3. The direction that actually fails does not need a transmitter

The failures measured here are **inbound**: device ACKs arriving at −103 to
−110 dBm while the PLM's own transmissions arrive at −51 to −68 dBm. The
asymmetry is structural — the PLM is a mains-powered transmitter surrounded
by the densest part of the repeater population, and battery devices transmit
once, weakly, with no repeat of their own.

For device → PLM, **injection strictly dominates RF repeating**: it needs no
transmitter, cannot collide with anything, cannot corrupt an in-flight
exchange, and is already built and running (`Doc/MESH-PLAN.md`). Transmitting
would only help PLM → device, which is the better-served direction.

---

## 4. Two cheaper things to try first

**Raise `max_hops` for the devices that need it.** insteon-mqtt is adaptively
using 2 hops for some devices. Forcing 3 adds a whole slot of flooding for
free, with no new hardware and no transmitter. This is the first thing to try
for any device that is missing commands.

**Buy Insteon range extenders for coverage gaps.** A 2443-222 (or any spare
dual-band plug-in module) is real Insteon silicon: it simulcasts correctly,
bridges RF to powerline — which an RF-only board fundamentally cannot do —
and is certified for the band. Around $30, and it solves the problem the
right way round. Keep the Heltecs for listening, which is what they are good
at and where they need no cooperation from the protocol.

---

## 5. The one niche where transmitting would genuinely help

A **last-resort repeater**, which avoids the simulcast problem by construction
rather than by solving it:

1. Hear a message with `hops_left = k > 0`.
2. Watch the next slot. If no `hops_left = k−1` copy appears, no repeater in
   range picked it up and the message is dying here.
3. Only then, transmit `hops_left = k−1` in a **later, empty** slot.

Nothing else is transmitting in that slot, by definition, so there is no
simulcast to align with — the frequency and timing problems both vanish.

**This repo already proves the receiving side works.** `insteon-rf send`
transmits a spoofed PLM message at an arbitrary moment and real i2cs devices
ACK it. Insteon receivers are not slot-locked for *reception*; they accept a
valid packet whenever it arrives. The slot grid governs transmission, not
acceptance.

### What it would have to refuse

- **Non-idempotent commands.** Devices must dedupe (they hear each message in
  up to four slots) but their window is short, so a late repeat can be
  processed a second time. Safe: On, Off, Status Request, Ping. **Never**:
  ALDB read/write (`0x2F`), peek/poke (`0x28`/`0x29`), begin/end all-linking
  (`0x64`/`0x65`), step bright/dim (`0x15`/`0x16`), set operating flags
  (`0x20`). Doubling an ALDB write can corrupt a link database.
- **Anything mid-exchange.** Transmitting while a device's ACK is in the air
  breaks the exchange being helped. The slot grid makes this avoidable —
  transmit only in a slot measured to be empty.
- **Group broadcasts** are the highest-value case (an unlinked group-0 all-on
  is invisible to the PLM entirely) and also the one where a duplicate is
  most visible to a human, since it re-triggers scenes.

### Cost

One transmitter added to a network that has exactly one today, a rate limit,
a command allowlist, a slot-tracking implementation on the ESP32, and RF
transmission from a board whose composite use is not certified for it. Set
against a niche that only fires when a device is out of range of *every*
existing repeater — which a $30 range extender fixes properly.

---

## 6. Recommendation

1. Force `max_hops = 3` on devices that miss commands. Free.
2. Add Insteon range extenders where coverage is genuinely missing. They
   simulcast correctly and bridge to powerline.
3. Finish the receive-side work: the miss table will say whether PLM → device
   is even a problem on this network. Every measurement so far says the
   failures are inbound, which injection already handles.
4. Keep the last-resort repeater in mind, but do not build it until (3) has
   produced a device that demonstrably cannot be reached any other way.

Transmit remains out of scope for the boards, and the firmware still has no
TX path linked.
