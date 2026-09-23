"""The injector's policy (``insteonrf.inject``).

Almost every test here asserts a *refusal*. This is the one component that
can make the Insteon protocol stack act on something the PLM never heard, so
the interesting question is never "does it work" but "does it decline when it
should".
"""

from __future__ import annotations

import pytest

from insteonrf.fusion import Fusion, sightings_from_capture
from insteonrf.inject import Injector, Tier, load_battery_addrs, sender_of
from insteonrf.packet import Address, Packet
from insteonrf.plm import from_plm_bytes, message_key, to_plm_bytes

PLM = "2B.93.07"
DEV = "29.4E.52"
LEAK = "44.12.AB"


def event(
    *,
    src=DEV,
    dst=PLM,
    group=None,
    receiver="a",
    timestamp=100.0,
    hops_left=3,
    **kw,
):
    """One fused event built the way the real path builds it."""
    kw.setdefault("cmd1", 0x11)
    kw.setdefault("cmd2", 0xFF)
    if group is not None:
        p = Packet.build(src, group=group, bcast=True, hops_left=hops_left, **kw)
    else:
        p = Packet.build(src, dst, hops_left=hops_left, **kw)
    f = Fusion()
    for s in sightings_from_capture(p.to_bits(), receiver, rssi_dbm=-70, timestamp=timestamp):
        f.add(s)
    events = f.flush(timestamp + 10)
    assert events, "fixture must produce an event"
    return events[0]


def injector(**kw) -> Injector:
    kw.setdefault("plm_addr", PLM)
    kw.setdefault("battery_addrs", [LEAK])
    kw.setdefault("allow", [Tier.BATTERY, Tier.GROUP, Tier.STATE])
    kw.setdefault("shadow", False)
    return Injector(**kw)


# --------------------------------------------------------------------------- happy path


def test_injects_a_battery_device_broadcast():
    sent = []
    inj = injector(publish=lambda frame, ev: sent.append(frame))
    d = inj.consider(event(src=LEAK, group=1, cmd1=0x11), now=100.0)
    assert d.inject is True
    assert d.tier is Tier.BATTERY
    assert sent and sent[0][:2] == b"\x02\x50"
    assert inj.counters.injected == 1


def test_the_injected_frame_decodes_back_to_the_same_message():
    inj = injector()
    ev = event(src=LEAK, group=4, cmd1=0x11)
    d = inj.consider(ev, now=100.0)
    assert message_key(from_plm_bytes(d.frame)) == message_key(ev.packet)


def test_group_zero_is_injectable():
    """The phantom all-on is exactly what the PLM filters out."""
    inj = injector()
    d = inj.consider(event(src=DEV, group=0, cmd1=0x11), now=100.0)
    assert d.inject is True and d.tier is Tier.GROUP


# --------------------------------------------------------------------------- refusals


def test_shadow_mode_decides_but_does_not_publish():
    sent = []
    inj = injector(shadow=True, publish=lambda frame, ev: sent.append(frame))
    d = inj.consider(event(src=LEAK, group=1), now=100.0)
    assert d.inject is False
    assert d.would_inject is True
    assert d.frame is not None, "the frame is still computed, for the log"
    assert sent == []
    assert inj.counters.would_inject == 1 and inj.counters.injected == 0


def test_shadow_is_the_default():
    assert Injector().shadow is True


def test_nothing_is_allowed_by_default():
    """An Injector with no tiers enabled injects nothing at all."""
    inj = Injector(plm_addr=PLM, shadow=False)
    assert inj.consider(event(src=DEV, group=1), now=100.0).inject is False


def test_kill_switch():
    inj = injector(enabled=lambda: False)
    d = inj.consider(event(src=LEAK, group=1), now=100.0)
    assert d.inject is False and d.reason == "kill switch"


@pytest.mark.parametrize(
    "kw",
    [
        {"ack": True},                    # DIRECT_ACK
        {"bcast": True, "ack": True},     # DIRECT_NAK
    ],
)
def test_replies_are_never_injected(kw):
    """An ACK answers a command insteon-mqtt is waiting on."""
    inj = injector()
    d = inj.consider(event(src=DEV, dst=PLM, **kw), now=100.0)
    assert d.inject is False
    assert d.reason == "reply to an outstanding command"


def test_the_plms_own_transmission_is_never_injected():
    """The listeners hear the modem too."""
    inj = injector()
    d = inj.consider(event(src=PLM, dst=DEV, cmd1=0x0D, cmd2=0x00), now=100.0)
    assert d.inject is False
    assert d.reason == "the PLM's own transmission"


def test_suppressed_when_the_plm_already_heard_it():
    inj = injector()
    ev = event(src=LEAK, group=1)
    inj.note_plm(to_plm_bytes(ev.packet), now=100.0)
    d = inj.consider(ev, now=100.3)
    assert d.inject is False
    assert d.reason == "the PLM already heard it"
    assert ev.plm_saw_it is True


def test_suppression_ignores_hops():
    """A hop repeat the PLM heard is the same message."""
    inj = injector()
    heard = event(src=LEAK, group=1, hops_left=3)
    inj.note_plm(to_plm_bytes(heard.packet), now=100.0)
    later = event(src=LEAK, group=1, hops_left=0)
    assert inj.consider(later, now=100.2).inject is False


def test_suppression_window_expires():
    """Past the window it is a new transmission, not a duplicate.

    The window is measured between the two paths' copies of one message, so
    it is the *message* that has to be later, not merely the moment the
    decision is taken: an event can wait a grace period before being settled
    and that must not age it out of its own suppression.
    """
    inj = injector(suppress_window_s=2.0)
    inj.note_plm(to_plm_bytes(event(src=LEAK, group=1).packet), now=100.0)
    d = inj.consider(event(src=LEAK, group=1, timestamp=105.0), now=105.0)
    assert d.inject is True
    assert d.reason == "injected"


def test_suppression_has_its_own_fixed_window():
    """Not insteon-mqtt's hops_left*0.087, which is zero at hops_left=0.

    Live evidence: two copies of one ACK at hops 1 and 0 were both processed
    by the modem 87 ms apart. Delegating suppression upstream would let a
    long-travelled message through twice.
    """
    inj = injector(suppress_window_s=2.0)
    ev = event(src=LEAK, group=1, hops_left=0)
    inj.note_plm(to_plm_bytes(ev.packet), now=100.0)
    assert inj.consider(ev, now=101.9).inject is False


def test_non_inbound_plm_frames_are_not_keyed():
    """0x62 is the modem echoing its own outbound command."""
    inj = injector()
    assert inj.note_plm(b"\x02\x62\x29\x4e\x52\x0f\x0d\x00\x06") is False
    assert inj.note_plm(b"") is False


def test_refuses_a_bad_crc():
    inj = injector()
    ev = event(src=LEAK, group=1)
    ev.packet.data[ev.packet.crc_index] ^= 0xFF
    d = inj.consider(ev, now=100.0)
    assert d.inject is False and d.reason == "CRC did not pass"


def test_refuses_rejected_frame_indexes():
    inj = injector()
    ev = event(src=LEAK, group=1)
    ev.packet.index_ok = False
    d = inj.consider(ev, now=100.0)
    assert d.inject is False and d.reason == "frame-index counters rejected"


def test_refuses_a_thinly_combined_packet():
    """A two-way vote ties on every disagreement; the CRC does all the work."""
    inj = injector(min_combined_receivers=3)
    ev = event(src=LEAK, group=1)
    ev.combined = True
    ev.combined_from = 2
    d = inj.consider(ev, now=100.0)
    assert d.inject is False
    assert "combined from only 2" in d.reason


def test_accepts_a_well_combined_packet():
    inj = injector(min_combined_receivers=3)
    ev = event(src=LEAK, group=1)
    ev.combined = True
    ev.combined_from = 3
    assert inj.consider(ev, now=100.0).inject is True


# --------------------------------------------------------------------------- rate limits


def test_per_device_rate_limit():
    inj = injector(per_device_interval_s=30.0)
    assert inj.consider(event(src=LEAK, group=1), now=100.0).inject is True
    d = inj.consider(event(src=LEAK, group=2), now=110.0)
    assert d.inject is False and d.reason == "per-device rate limit"
    assert inj.consider(event(src=LEAK, group=3), now=131.0).inject is True


def test_global_rate_limit_protects_outbound_commands():
    """Inbound messages push out insteon-mqtt's next allowed transmit."""
    inj = injector(global_per_minute=3, per_device_interval_s=0.0)
    devices = ["44.12.A1", "44.12.A2", "44.12.A3", "44.12.A4"]
    inj.battery_addrs |= {Address(a) for a in devices}
    got = [inj.consider(event(src=d, group=1), now=100.0 + i).inject
           for i, d in enumerate(devices)]
    assert got == [True, True, True, False]
    assert inj.counters.refused["global rate limit"] == 1


def test_global_rate_limit_is_a_sliding_window():
    inj = injector(global_per_minute=1, per_device_interval_s=0.0)
    assert inj.consider(event(src=LEAK, group=1), now=100.0).inject is True
    assert inj.consider(event(src=LEAK, group=2), now=130.0).inject is False
    assert inj.consider(event(src=LEAK, group=3), now=170.0).inject is True


# --------------------------------------------------------------------------- tiers


def test_tiers_are_enabled_one_at_a_time():
    only_battery = injector(allow=[Tier.BATTERY])
    assert only_battery.consider(event(src=LEAK, group=1), now=100.0).inject is True
    d = only_battery.consider(event(src=DEV, group=1), now=101.0)
    assert d.inject is False and d.reason == "tier GROUP not enabled"


def test_state_tier_covers_unsolicited_direct_messages():
    inj = injector(allow=[Tier.STATE])
    d = inj.consider(event(src=DEV, dst=PLM, cmd1=0x11, cmd2=0xFF), now=100.0)
    assert d.inject is True and d.tier is Tier.STATE


def test_battery_classification_wins_over_group():
    inj = injector(allow=[Tier.BATTERY])
    d = inj.consider(event(src=LEAK, group=1), now=100.0)
    assert d.tier is Tier.BATTERY


# --------------------------------------------------------------------------- helpers


def test_sender_of_handles_both_address_slots():
    direct = Packet.build(DEV, PLM, cmd1=0x11, cmd2=0x00)
    group = Packet.build(DEV, group=3, bcast=True, cmd1=0x11, cmd2=0x00)
    assert sender_of(direct) == Address(DEV)
    assert group.from_addr is None, "a group broadcast has no from_addr"
    assert sender_of(group) == Address(DEV), "the sender rides in the to slot"


def test_load_battery_addrs(tmp_path):
    p = tmp_path / "battery.txt"
    p.write_text("# leak sensors\n44.12.AB\n1C.5B.D0   # a remote\n\n")
    assert load_battery_addrs(str(p)) == {Address("44.12.AB"), Address("1C.5B.D0")}


def test_counters_report_why_things_were_refused():
    inj = injector(allow=[Tier.BATTERY])
    inj.consider(event(src=DEV, dst=PLM, ack=True), now=100.0)
    inj.consider(event(src=PLM, dst=DEV), now=101.0)
    inj.consider(event(src=DEV, group=1), now=102.0)
    d = inj.counters.to_dict()
    assert d["considered"] == 3 and d["injected"] == 0
    assert d["refused"]["reply to an outstanding command"] == 1
    assert d["refused"]["the PLM's own transmission"] == 1
    assert d["refused"]["tier GROUP not enabled"] == 1


def test_suppression_is_anchored_on_the_message_not_the_decision():
    """An event waits a grace period before being settled, so that the
    modem's copy has time to arrive. If the suppression lookup used the
    moment of the decision instead of the message's own time, that wait
    would push every message out of its own window and the injector would
    hand insteon-mqtt messages it had already reported."""
    inj = injector(suppress_window_s=2.0)
    p = event(src=LEAK, group=1, timestamp=100.0)
    inj.note_plm(to_plm_bytes(p.packet), now=100.4)
    d = inj.consider(event(src=LEAK, group=1, timestamp=100.0), now=102.6)
    assert d.inject is False
    assert d.reason == "the PLM already heard it"


def test_the_modem_may_report_a_message_after_the_rf_copy():
    """Measured: the modem's copy landed up to 1.3 s after the first RF
    sighting for one message in five. The window is two-sided for that."""
    inj = injector(suppress_window_s=2.0)
    p = event(src=LEAK, group=1, timestamp=100.0)
    inj.note_plm(to_plm_bytes(p.packet), now=101.3)      # modem, later
    assert inj.consider(event(src=LEAK, group=1, timestamp=100.0), now=103.0).inject is False
    inj2 = injector(suppress_window_s=2.0)
    inj2.note_plm(to_plm_bytes(p.packet), now=98.7)      # modem, earlier
    assert inj2.consider(event(src=LEAK, group=1, timestamp=100.0), now=103.0).inject is False


def test_an_address_this_network_does_not_have_is_never_injected():
    """A decoder that occasionally invents a packet invents its sender too,
    and insteon-mqtt would create state for a device that does not exist."""
    inj = injector(allow=[Tier.GROUP], known_addrs=[DEV])
    d = inj.consider(event(src="AC.4C.1E", group=1))
    assert d.inject is False
    assert d.reason == "sender is not a device on this network"
    assert inj.consider(event(src=DEV, group=1)).inject is True


def test_no_list_means_no_check():
    """The old behaviour, kept so the measurement phase is unaffected -- but
    the mesh command warns when injection runs without a list."""
    inj = injector(allow=[Tier.GROUP])
    assert inj.known_addrs is None
    assert inj.consider(event(src="AC.4C.1E", group=1)).inject is True


def test_impossible_hop_fields_are_refused_at_the_injector_too():
    """Defence in depth: fusion should never hand one over, and the PLM
    conversion would refuse it, but this is the decision that matters."""
    inj = injector(allow=[Tier.GROUP])
    d = inj.consider(event(src=DEV, group=1, hops_left=2, max_hops=0))
    assert d.inject is False
    assert d.reason == "impossible hop fields"
