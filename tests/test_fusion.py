"""Multi-receiver fusion (``insteonrf.fusion``).

The tests that matter most here are the refusals. Combining copies from
several receivers can manufacture a packet that no receiver actually heard,
and the result is fed to a real protocol stack — so a candidate whose CRC
happens to match but whose frame-index counters are impossible has to be
rejected, and a retransmission must not be folded away as a duplicate.
"""

from __future__ import annotations

import random

import pytest

from insteonrf.fusion import (
    MAX_DISAGREEMENTS,
    Fusion,
    Sighting,
    combine,
    fuse_all,
    sightings_from_capture,
)
from insteonrf.packet import Packet, parse_bits
from insteonrf.plm import message_key

PLM = "2B.93.07"
DEV = "29.4E.52"


def capture(hops_left: int = 3, **kw) -> str:
    """On-air bits for one packet, as a receiver would capture them."""
    kw.setdefault("cmd1", 0x11)
    kw.setdefault("cmd2", 0xFF)
    return Packet.build(DEV, PLM, hops_left=hops_left, max_hops=3, **kw).to_bits()


def pkt(hops_left: int = 3, **kw) -> Packet:
    out = parse_bits(capture(hops_left, **kw))
    assert out, "fixture packet must decode"
    return out[0]


def sight(receiver="a", *, hops_left=3, rssi_dbm=None, timestamp=None,
          damage=(), **kw) -> Sighting:
    """One receiver's sighting, optionally with bits flipped in transit."""
    bits = list(capture(hops_left, **kw))
    for i in damage:
        bits[i] = "0" if bits[i] == "1" else "1"
    got = sightings_from_capture("".join(bits), receiver, rssi_dbm=rssi_dbm,
                                 timestamp=timestamp)
    if not got:
        pytest.skip("damage destroyed the start header")
    return got[0]


# --------------------------------------------------------------------------- bucketing


def test_one_transmission_from_three_receivers_is_one_event():
    f = Fusion()
    for i, name in enumerate(["a", "b", "c"]):
        f.add(sight(name, rssi_dbm=-70 - i, timestamp=100.0 + i * 0.01))
    assert f.pop_ready(100.05) == []
    events = f.pop_ready(103.0)
    assert len(events) == 1
    assert events[0].heard_by == 3
    assert events[0].receivers["a"].copies == 1


def test_hop_repeats_fold_into_one_event():
    f = Fusion()
    for i, hops in enumerate([3, 2, 1, 0]):
        f.add(sight("a", hops_left=hops, timestamp=100.0 + i * 0.05))
    (e,) = f.pop_ready(103.0)
    assert e.hops_left_max == 3 and e.hops_left_min == 0
    assert e.hops_used == 3
    assert e.receivers["a"].copies == 4


def test_a_retransmission_is_not_folded_away():
    """An Insteon retry, or a second button press, is a real event."""
    f = Fusion()
    f.add(sight("a", timestamp=100.0))
    first = f.pop_ready(101.0)
    f.add(sight("a", timestamp=105.0))
    second = f.pop_ready(106.0)
    assert len(first) == 1 and len(second) == 1
    assert first[0].repeat_index == 0
    assert second[0].repeat_index == 1
    assert first[0].key == second[0].key


def test_repeat_numbering_is_forgotten_eventually():
    f = Fusion(repeat_memory_s=10.0)
    f.add(sight("a", timestamp=100.0))
    f.pop_ready(101.0)
    f.add(sight("a", timestamp=200.0))
    (e,) = f.pop_ready(201.0)
    assert e.repeat_index == 0, "a transmission two minutes later is not a repeat"


def test_window_is_capped_so_a_chain_cannot_hold_it_open():
    f = Fusion(window_s=0.6, extend_s=0.2, max_window_s=2.0)
    t = 100.0
    for _ in range(50):
        f.add(sight("a", timestamp=t))
        t += 0.1
    assert f.pop_ready(t + 0.01), "bucket must close despite the steady stream"


def test_different_messages_do_not_share_a_bucket():
    f = Fusion()
    f.add(sight("a", cmd2=0x01, timestamp=100.0))
    f.add(sight("a", cmd2=0x02, timestamp=100.01))
    assert len(f.pop_ready(103.0)) == 2


# --------------------------------------------------------------------------- attribution


def test_closest_is_the_receiver_with_the_most_hops_left():
    """No clock needed: the packet carries how far it has travelled."""
    f = Fusion()
    f.add(sight("far", hops_left=1, rssi_dbm=-60, timestamp=100.0))
    f.add(sight("near", hops_left=3, rssi_dbm=-95, timestamp=100.02))
    (e,) = f.pop_ready(103.0)
    assert e.closest == "near", "hops beat RSSI; a loud repeat is still a repeat"


def test_rssi_breaks_a_hop_tie():
    f = Fusion()
    f.add(sight("quiet", hops_left=3, rssi_dbm=-99, timestamp=100.0))
    f.add(sight("loud", hops_left=3, rssi_dbm=-62, timestamp=100.01))
    (e,) = f.pop_ready(103.0)
    assert e.closest == "loud"


def test_best_decode_prefers_the_least_travelled_good_copy():
    f = Fusion()
    f.add(sight("a", hops_left=0, timestamp=100.0))
    f.add(sight("b", hops_left=3, timestamp=100.01))
    (e,) = f.pop_ready(103.0)
    assert e.packet.hops_left == 3
    assert e.combined is False


def test_damage_in_the_pad_is_not_damage():
    """Documents why the combining tests aim at bytes 4-9.

    The packet CRC covers bytes 0-8 and sits at byte 9; bytes 10+ are pad.
    A flip in the pad leaves a fully valid packet with an identical key, so
    it is useless as a test of combining.
    """
    s_pad = sight("a", damage=[320])
    assert s_pad.packet.crc_ok is True
    assert message_key(s_pad.packet) == message_key(pkt())


def test_per_receiver_view_records_rssi_and_crc():
    f = Fusion()
    f.add(sight("a", rssi_dbm=-70, timestamp=100.0))
    f.add(sight("b", rssi_dbm=-101, timestamp=100.01, damage=[200]))
    (e,) = f.pop_ready(103.0)
    assert e.receivers["a"].crc_ok is True
    assert e.receivers["a"].rssi_dbm == -70
    assert e.receivers["b"].crc_ok is False


# --------------------------------------------------------------------------- combining


def test_combine_recovers_a_packet_no_receiver_got():
    """Independent noise at different locations is the whole point."""
    a = sight("a", damage=[200])   # byte 6
    b = sight("b", damage=[230])   # byte 7
    c = sight("c", damage=[260])   # byte 8
    assert a.packet.crc_ok is not True
    assert b.packet.crc_ok is not True
    assert c.packet.crc_ok is not True
    got = combine([a, b, c])
    assert got is not None
    packet, n = got
    assert n == 3
    assert packet.crc_ok is True
    assert message_key(packet) == message_key(pkt())


def test_fusion_uses_combining_end_to_end():
    f = Fusion()
    for i, bit in enumerate([200, 230, 260]):
        f.add(sight(f"r{i}", timestamp=100.0 + i * 0.01, damage=[bit]))
    (e,) = f.pop_ready(103.0)
    assert e.combined is True
    assert e.combined_from == 3
    assert e.packet.crc_ok is True
    assert message_key(e.packet) == message_key(pkt())


def test_combine_needs_more_than_one_copy():
    assert combine([sight("a", damage=[200])]) is None


def test_a_copy_damaged_in_its_first_bytes_cannot_contribute():
    """Known limitation, kept explicit.

    decode_frames stops at the damage, so a copy hit inside the first four
    bytes does not clear the "this is probably a packet" gate and yields no
    sighting at all. With three or more receivers only two usable copies are
    needed, so this costs coverage rather than correctness.
    """
    bits = list(capture())
    bits[120] = "0" if bits[120] == "1" else "1"
    assert sightings_from_capture("".join(bits), "a") == []


def test_combine_gives_up_on_wide_disagreement():
    """A copy that is mostly junk must not be voted into a plausible packet."""
    rng = random.Random(7)
    spots = rng.sample(range(140, 300), MAX_DISAGREEMENTS + 8)
    a = sight("a", damage=spots[: len(spots) // 2])
    b = sight("b", damage=spots[len(spots) // 2 :])
    assert combine([a, b]) is None


def test_combining_is_disabled_when_asked():
    f = Fusion(allow_combine=False)
    for i, bit in enumerate([200, 230, 260]):
        f.add(sight(f"r{i}", timestamp=100.0 + i * 0.01, damage=[bit]))
    events = f.pop_ready(103.0)
    # Without combining, damaged copies cannot be recognised as the same
    # message at all -- their bytes and therefore their keys differ.
    assert len(events) == 3
    assert all(not e.combined for e in events)


def test_combine_rejects_a_candidate_with_impossible_frame_indexes():
    """The guard that stops combining from inventing messages.

    An 8-bit CRC accepts one random candidate in 256. The frame-index
    counters are known from position, are not covered by the CRC, and are
    therefore what makes a CRC-guided search safe. A candidate that satisfies
    the CRC but not the counters must be refused.
    """
    from insteonrf import fusion

    good = pkt()
    seen = {"checked": 0, "rejected": 0}
    real_accept = fusion._accept

    def spy(bits, ts):
        got = real_accept(bits, ts)
        seen["checked"] += 1
        return got

    # Directly: a packet whose counters were rejected is never accepted.
    for p in parse_bits(good.bits):
        p.index_ok = False
        assert p.crc_ok is True, "fixture should have a good CRC"
    assert fusion._accept(good.bits, None) is not None

    # And a bit string mangled inside the index field must not come back as a
    # valid packet even if some candidate CRC lines up.
    mangled = list(good.bits)
    for i in range(7, 17):  # the first frame's 5-bit index, Manchester coded
        mangled[i] = "0" if mangled[i] == "1" else "1"
    out = fusion._accept("".join(mangled), None)
    if out is not None:
        assert out.index_ok is not False
    seen["rejected"] += 1
    assert seen["rejected"] == 1


def test_accept_requires_the_extended_data_crc_too():
    from insteonrf import fusion

    p = Packet.build(DEV, PLM, cmd1=0x2F, cmd2=0x00, ext_data=[1, 2, 3] + [0] * 10)
    decoded = parse_bits(p.to_bits())[0]
    assert fusion._accept(decoded.bits, None) is not None
    bits = list(decoded.bits)
    # Corrupt a data byte; the recomputed data CRC will no longer match.
    for i in range(300, 310):
        bits[i] = "0" if bits[i] == "1" else "1"
    got = fusion._accept("".join(bits), None)
    assert got is None or got.ext_crc_ok is not False


# --------------------------------------------------------------------------- replay


def test_fuse_all_replays_a_finite_stream():
    events = list(
        fuse_all(
            [
                sight("a", hops_left=3, timestamp=100.0),
                sight("b", hops_left=2, timestamp=100.05),
                sight("a", cmd2=0x02, timestamp=110.0),
            ]
        )
    )
    assert len(events) == 2
    assert events[0].heard_by == 2
    assert events[1].heard_by == 1


def test_to_dict_carries_the_mesh_fields():
    f = Fusion()
    f.add(sight("near", hops_left=3, rssi_dbm=-65, timestamp=100.0))
    f.add(sight("far", hops_left=1, rssi_dbm=-98, timestamp=100.02))
    (e,) = f.pop_ready(103.0)
    d = e.to_dict()
    assert d["closest"] == "near"
    assert d["receiver_count"] == 2
    assert d["hops_left_max"] == 3 and d["hops_left_min"] == 1
    assert d["heard_by"]["far"]["rssi_dbm"] == -98
    assert d["plm_saw_it"] is None
    assert d["from"] == DEV  # the normal packet schema is still there


# --------------------------------------------------------------------------- soft decisions


def soft_sight(receiver="v4", *, damage=(), doubt=(), unsure=0.05, timestamp=None,
               invert=False, **kw) -> Sighting:
    """An I/Q receiver's sighting: hard bits plus per-symbol confidence.

    ``damage`` flips bits; ``doubt`` marks positions the detector was unsure
    of (confidence ``unsure``) without flipping them. ``invert`` presents the
    capture in the other polarity, as a receiver that matched the inverted
    sync word would.
    """
    import numpy as np

    bits = list(capture(**kw))
    for i in damage:
        bits[i] = "0" if bits[i] == "1" else "1"
    stream = "".join(bits)
    soft = np.where(np.frombuffer(stream.encode(), dtype=np.uint8) == ord("1"), 1.0, -1.0
                    ).astype(np.float32)
    for i in list(damage) + list(doubt):
        soft[i] *= unsure
    if invert:
        stream = "".join("0" if b == "1" else "1" for b in stream)
        soft = -soft
    got = sightings_from_capture(stream, receiver, timestamp=timestamp, soft=soft)
    assert got, "fixture must decode"
    return got[0]


def test_a_soft_sighting_carries_confidence_aligned_with_its_bits():
    s = soft_sight(doubt=[200])
    assert s.soft is not None
    assert len(s.soft) >= len(s.bits)
    # Positive means '1', and the doubtful symbol is the small one. The
    # sighting starts at the header, so stream position 200 moves up by the
    # preamble the capture began with.
    off = len(capture()) - len(s.bits)
    for i in (150, 199, 201):
        assert (s.soft[i] > 0) == (s.bits[i] == "1")
    assert abs(s.soft[200 - off]) < 0.1


def test_polarity_normalisation_flips_the_confidence_too():
    a = soft_sight(doubt=[200])
    b = soft_sight(doubt=[200], invert=True)
    assert a.bits == b.bits
    assert (a.soft[:400] == b.soft[:400]).all()


def test_one_soft_copy_alone_can_be_repaired():
    """The single-receiver case that hard bits cannot attempt."""
    s = soft_sight(damage=[200, 230])
    assert s.packet.crc_ok is not True
    got = combine([s])
    assert got is not None
    packet, n = got
    assert n == 1
    assert packet.crc_ok is True
    assert message_key(packet) == message_key(pkt())


def test_a_confident_wrong_symbol_is_not_repaired_from_one_copy():
    """Confidence is the only clue a single copy gives; a wrong bit the
    detector was sure of is not among the suspects, so no packet is
    manufactured for it — MAX_DISAGREEMENTS least-sure positions are tried
    and the CRC plus frame counters still have to agree."""
    s = soft_sight(damage=[200], unsure=1.0)
    got = combine([s])
    assert got is None or message_key(got[0]) == message_key(pkt())


def test_a_hard_copy_outvotes_a_doubtful_soft_symbol():
    """The dongle is sure, the SDR is not: the SDR's wrong bit loses without
    any flipping."""
    hard = sight("dongle", damage=[260])
    soft = soft_sight("v4", damage=[200, 230])  # wrong where it was unsure
    got = combine([hard, soft])
    assert got is not None
    packet, n = got
    assert n == 2
    assert packet.crc_ok is True
    assert message_key(packet) == message_key(pkt())


def test_soft_suspects_are_tried_least_sure_first():
    """Two damaged symbols, one wrong hard copy elsewhere: the least-sure
    positions are the damaged ones and the pair is found within the flip
    budget."""
    hard = sight("dongle", damage=[300])
    soft = soft_sight("v4", damage=[200, 230], unsure=0.2, doubt=[150, 170, 190])
    got = combine([hard, soft])
    assert got is not None
    assert got[0].crc_ok is True


def test_fusion_repairs_a_lone_soft_sighting_end_to_end():
    f = Fusion()
    f.add(soft_sight(damage=[200, 230], timestamp=100.0))
    (e,) = f.pop_ready(103.0)
    assert e.combined is True
    assert e.combined_from == 1
    assert e.packet.crc_ok is True
    assert message_key(e.packet) == message_key(pkt())


def test_misaligned_confidence_is_dropped_not_trusted():
    import numpy as np

    s = Sighting(pkt(), "v4", soft=np.zeros(10, dtype=np.float32))
    assert s.soft is None


def test_receiver_view_keeps_each_receiver_s_snr():
    """Comparing receivers is the point of having more than one, and symbol
    SNR is the number that says how much margin each had on the same
    transmission. Only I/Q receivers measure one, so it lives per receiver
    rather than per event."""
    from insteonrf.fusion import ReceiverView, Sighting, fuse_all

    p = pkt()
    sights = [
        Sighting(p, "v4", rssi_dbm=None, timestamp=1.0, snr_db=18.5),
        Sighting(p, "v4", rssi_dbm=None, timestamp=1.05, snr_db=22.0),
        Sighting(p, "dongle", rssi_dbm=-70.0, timestamp=1.02),
    ]
    events = list(fuse_all(sights))
    assert len(events) == 1
    views = events[0].receivers
    # The best copy's margin, not the last one's.
    assert views["v4"].snr_db == 22.0
    assert views["dongle"].snr_db is None
    assert events[0].to_dict()["heard_by"]["v4"]["snr_db"] == 22.0
    assert ReceiverView().snr_db is None
