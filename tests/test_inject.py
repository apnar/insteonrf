"""The insteon-mqtt patch in ``deploy/insteon-mqtt/``.

These tests apply the patch series to a pristine upstream tree and exercise
the result, so they prove two separate things: that the patch still applies,
and that ``Protocol.inject()`` behaves. Both matter — the patch is carried
rather than merged, so a version bump must fail loudly here rather than
silently on the running house.

Run with::

    kubectl exec homeassistant -c insteon -- tar cf - -C /opt/insteon-mqtt \\
        insteon_mqtt | tar xf - -C /tmp/imqtt
    INSTEONRF_IMQTT=/tmp/imqtt pytest tests/test_inject.py
"""

from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from insteonrf.packet import Packet
from insteonrf.plm import to_plm_bytes

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATCH_DIR = ROOT / "deploy" / "insteon-mqtt"

PLM = "2B.93.07"
DEV = "29.4E.52"

pytestmark = pytest.mark.imqtt


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def patched(tmp_path_factory):
    """A patched insteon-mqtt tree, imported under a stub parent package.

    The stub parent keeps ``insteon_mqtt/__init__.py`` (which wants jinja2 and
    the whole MQTT layer) out of the way; only the protocol modules load.
    """
    root = os.environ.get("INSTEONRF_IMQTT")
    if not root:
        pytest.skip("set INSTEONRF_IMQTT to an insteon-mqtt source tree")
    if not shutil.which("patch"):
        pytest.skip("needs the 'patch' utility")
    src = pathlib.Path(root)
    if not (src / "config-example.yaml").is_file():
        pytest.skip(f"{root} has no config-example.yaml; extract it alongside insteon_mqtt")

    work = tmp_path_factory.mktemp("imqtt") / "src"
    shutil.copytree(src, work)

    patches = sorted(PATCH_DIR.glob("*.patch"))
    assert patches, f"no patches found in {PATCH_DIR}"
    for patch in patches:
        # --forward turns "already applied" into an error rather than a
        # reverse-apply, and -p1 matches the a/ b/ prefixes in the series.
        proc = subprocess.run(
            ["patch", "-p1", "--forward", "--no-backup-if-mismatch", "-i", str(patch)],
            cwd=work,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            pytest.fail(
                f"{patch.name} does not apply to this upstream tree:\n"
                f"{proc.stdout}\n{proc.stderr}"
            )

    # Load under a unique package name so a pristine insteon_mqtt imported by
    # other tests in the same session is not shadowed.
    name = "imqtt_patched"
    stub = types.ModuleType(name)
    stub.__path__ = [str(work / "insteon_mqtt")]
    sys.modules[name] = stub
    # The tree's own relative imports say "insteon_mqtt", so alias both.
    sys.modules["insteon_mqtt"] = stub
    for mod in [m for m in sys.modules if m.startswith("insteon_mqtt.")]:
        del sys.modules[mod]

    proto = importlib.import_module("insteon_mqtt.Protocol")
    msg = importlib.import_module("insteon_mqtt.message")
    return types.SimpleNamespace(Protocol=proto.Protocol, message=msg, root=work)


class FakeLink:
    """Stands in for network.Serial: the two signals Protocol connects to."""

    def __init__(self, Signal):
        self.signal_read = Signal()
        self.signal_wrote = Signal()
        self.written: list[bytes] = []

    def poll(self, t=None):
        pass

    def write(self, data):
        self.written.append(bytes(data))


class Recorder:
    """Collects what the protocol emits.

    Not a lambda on purpose: upstream's ``Signal.connect`` stores a *weak*
    reference to the slot, so a lambda is collected the moment connect()
    returns and the signal silently does nothing. A bound method of an object
    we keep alive works, which is why the recorder is attached to the
    protocol below.
    """

    def __init__(self):
        self.msgs = []
        self.raw = []

    def on_msg(self, msg):
        self.msgs.append(msg)

    def on_raw(self, raw, duplicate):
        self.raw.append((raw, duplicate))


@pytest.fixture
def protocol(patched):
    signal_mod = importlib.import_module("insteon_mqtt.Signal")
    link = FakeLink(signal_mod.Signal)
    p = patched.Protocol(link)
    rec = Recorder()
    p.rec = rec  # keep it alive; Signal only holds a weak reference
    p.signal_received.connect(rec.on_msg)
    p.signal_raw_read.connect(rec.on_raw)
    return p


def frame(**kw) -> bytes:
    kw.setdefault("cmd1", 0x11)
    kw.setdefault("cmd2", 0xFF)
    return to_plm_bytes(Packet.build(DEV, PLM, **kw))


# --------------------------------------------------------------------------- inject


def test_inject_processes_a_standard_message(protocol):
    assert protocol.inject(frame()) is True
    assert len(protocol.rec.msgs) == 1
    msg = protocol.rec.msgs[0]
    assert str(msg.from_addr).upper() == DEV
    assert msg.cmd1 == 0x11 and msg.cmd2 == 0xFF


def test_inject_processes_a_group_broadcast(protocol):
    raw = to_plm_bytes(Packet.build(DEV, group=7, bcast=True, cmd1=0x11, cmd2=0xFF))
    assert protocol.inject(raw) is True
    assert protocol.rec.msgs[0].group == 7


def test_inject_processes_an_extended_message(protocol):
    raw = to_plm_bytes(
        Packet.build(DEV, PLM, cmd1=0x2F, cmd2=0x00, ext_data=[1, 2, 3] + [0] * 10)
    )
    assert protocol.inject(raw) is True
    assert list(protocol.rec.msgs[0].data)[:3] == [1, 2, 3]


def test_inject_never_touches_the_read_buffer(protocol):
    """The serial link's buffer is shared; interleaving would corrupt both."""
    protocol._buf.extend(b"\x02\x50\xde\xad")  # a half-received real frame
    before = bytes(protocol._buf)
    protocol.inject(frame())
    assert bytes(protocol._buf) == before


def test_injected_duplicate_of_a_modem_message_is_dropped(protocol):
    """The whole reason injection is safe: upstream's own dedup catches it."""
    raw = frame(hops_left=3)
    protocol.link.signal_read.emit(protocol.link, raw)  # as if the modem read it
    assert len(protocol.rec.msgs) == 1
    assert protocol.inject(raw) is False
    assert len(protocol.rec.msgs) == 1, "must not be processed twice"


def test_injected_duplicate_ignores_hops(protocol):
    protocol.link.signal_read.emit(protocol.link, frame(hops_left=3))
    assert protocol.inject(frame(hops_left=1)) is False


def test_no_hops_left_means_no_duplicate_window(protocol):
    """Documents the trap that makes injector-side suppression mandatory.

    The window is ``hops_left * 0.087``, so a copy that arrives with no hops
    left expires immediately and upstream will happily process a second copy.
    Suppression therefore cannot be left to insteon-mqtt alone.
    """
    raw = frame(hops_left=0, max_hops=0)
    protocol.link.signal_read.emit(protocol.link, raw)
    assert len(protocol.rec.msgs) == 1
    assert protocol.inject(raw) is True, "no window at hops_left=0"
    assert len(protocol.rec.msgs) == 2


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x02",
        b"\x03\x50" + b"\x00" * 9,  # wrong STX
        b"\x02\x62" + b"\x00" * 9,  # outbound message code
        b"\x02\x60" + b"\x00" * 9,  # modem info, not an inbound message
        b"\x02\x50" + b"\x00" * 4,  # too short
        b"\x02\x51" + b"\x00" * 9,  # extended, too short
    ],
)
def test_inject_refuses_malformed_frames(protocol, raw):
    assert protocol.inject(raw) is False
    assert protocol.rec.msgs == []


def test_inject_cannot_send_a_command_to_the_modem(protocol):
    """An injected frame must not be able to make the modem do anything."""
    assert protocol.inject(b"\x02\x62\x29\x4e\x52\x0f\x11\xff") is False
    assert protocol.link.written == []


# --------------------------------------------------------------------------- publish side


def test_raw_signal_fires_for_modem_reads(protocol):
    raw = frame()
    protocol.link.signal_read.emit(protocol.link, raw)
    assert protocol.rec.raw == [(raw, False)]


def test_raw_signal_marks_duplicates(protocol):
    raw = frame(hops_left=3)
    protocol.link.signal_read.emit(protocol.link, raw)
    protocol.link.signal_read.emit(protocol.link, raw)
    assert [dup for _, dup in protocol.rec.raw] == [False, True]


def test_raw_signal_carries_the_exact_frame(protocol):
    """The published bytes must round-trip, or the receiver cannot key on them."""
    from insteonrf.plm import from_plm_bytes, message_key

    p = Packet.build(DEV, group=4, bcast=True, cmd1=0x06, cmd2=0x01)
    protocol.link.signal_read.emit(protocol.link, to_plm_bytes(p))
    raw, _ = protocol.rec.raw[0]
    assert message_key(from_plm_bytes(raw)) == message_key(p)


def test_raw_signal_fires_for_partial_then_complete_reads(protocol):
    """Frames arrive split across serial reads; the signal must see them whole."""
    raw = frame()
    protocol.link.signal_read.emit(protocol.link, raw[:4])
    assert protocol.rec.raw == []
    protocol.link.signal_read.emit(protocol.link, raw[4:])
    assert protocol.rec.raw == [(raw, False)]
