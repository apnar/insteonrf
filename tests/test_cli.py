import io
import pathlib
import subprocess
import sys

import pytest

from insteonrf import cli


def run(func, argv, stdin=""):
    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), io.StringIO(), io.StringIO()
    try:
        rc = func(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err


def test_pkt_then_print():
    rc, bits, _ = run(cli.pkt_main, ["-s", "13.25.80", "-d", "16.3F.E5", "13", "00"])
    assert rc == 0 and set(bits.strip()) <= {"0", "1"}
    rc, out, _ = run(cli.print_main, [], stdin=bits)
    assert out.strip() == "0F : E5 3F 16 : 80 25 13 : 13 00 36 00 00 AA     crc 36"


def test_pkt_raw_matches_doc_example():
    rc, bits, _ = run(cli.pkt_main, ["-r", "0B", "E5", "3F", "16", "80", "25", "13", "11", "BF"])
    rc, out, _ = run(cli.print_main, ["-v"], stdin=bits)
    assert out.startswith("0B : E5 3F 16 : 80 25 13 : 11 BF 5F 00 00 AA     crc 5F")
    assert "13.25.80 -> 16.3F.E5  On (0x11 0xBF)" in out


def test_pkt_raw_with_colons_and_count():
    rc, bits, _ = run(cli.pkt_main, ["-r", "-c", "3", "0B", ":", "E5", "3F", "16", ":", "80", "25", "13", ":", "11", "BF"])
    assert bits.count("\n") == 3


def test_pkt_dry_run_describes():
    rc, out, err = run(cli.pkt_main, ["-n", "-s", "2B.93.07", "-d", "29.4E.52", "0F"])
    assert rc == 0 and out == "" and "Ping" in err


def test_pkt_errors():
    with pytest.raises(SystemExit):
        run(cli.pkt_main, ["-d", "16.3F.E5", "11"])          # no src
    with pytest.raises(SystemExit):
        run(cli.pkt_main, ["-s", "13.25.80", "11"])           # no dst/group


def test_print_passes_meta_and_time(tmp_path):
    _, bits, _ = run(cli.pkt_main, ["-s", "13.25.80", "-d", "16.3F.E5", "11", "FF"])
    log = tmp_path / "log.txt"
    rc, out, _ = run(cli.print_main, ["-t", "-l", str(log)], stdin="# hello\n" + bits)
    lines = out.splitlines()
    assert lines[0] == "# hello" and lines[1].endswith("crc 78") or lines[1].endswith("crc " + lines[1][-2:])
    assert log.read_text().strip() == "0F : E5 3F 16 : 80 25 13 : 11 FF 78 00 00 AA".replace("78", f"{int(lines[1][-2:], 16):02X}")


def test_dump_runs():
    _, bits, _ = run(cli.pkt_main, ["-s", "13.25.80", "-d", "16.3F.E5", "11", "FF"])
    rc, out, _ = run(cli.dump_main, [], stdin=bits)
    assert rc == 0 and "crc OK" in out


def test_console_script_help():
    r = subprocess.run([sys.executable, "-m", "insteonrf.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "recv" in r.stdout


# --------------------------------------------------------------------------- new commands


def test_print_json():
    _, bits, _ = run(cli.pkt_main, ["-s", "13.25.80", "-d", "16.3F.E5", "11", "FF"])
    rc, out, _ = run(cli.print_main, ["-j"], stdin="# meta\n" + bits)
    import json

    rec = json.loads(out.strip())
    assert rec["from"] == "13.25.80" and rec["command"] == "On" and rec["crc_ok"] is True
    assert out.count("\n") == 1  # the '# meta' line is suppressed in JSON mode


def test_pkt_json():
    import json

    rc, out, _ = run(cli.pkt_main, ["-j", "-s", "2B.93.07", "-d", "29.4E.52", "0F", "00"])
    assert rc == 0 and json.loads(out)["command"] == "Ping"


def test_modulate_then_demod(tmp_path):
    import numpy as np

    from insteonrf.packet import parse_bits

    _, bits, _ = run(cli.pkt_main, ["-s", "2B.93.07", "-d", "29.4E.52", "0F", "00"])
    iq = tmp_path / "ping.iq"
    # modulate reads bit lines on stdin and writes raw I/Q
    old = sys.stdin
    sys.stdin = io.StringIO(bits)
    try:
        assert cli.modulate_main(["-o", str(iq)]) == 0
    finally:
        sys.stdin = old
    raw = np.fromfile(iq, dtype=np.int8)
    assert raw.size > 1000

    rc, out, _ = run(cli.demod_main, ["--demod", "numpy", str(iq)])
    assert rc == 0
    pkts = [p for line in out.splitlines() if line[:1] in "01" for p in parse_bits(line)]
    assert any(p.cmd_name == "Ping" and p.crc_ok for p in pkts)


def test_demod_c_backend_matches(tmp_path, fsk2_demod):
    from insteonrf.packet import parse_bits

    _, bits, _ = run(cli.pkt_main, ["-s", "2B.93.07", "-d", "29.4E.52", "0F", "00"])
    iq = tmp_path / "ping.iq"
    old = sys.stdin
    sys.stdin = io.StringIO(bits)
    try:
        cli.modulate_main(["-o", str(iq)])
    finally:
        sys.stdin = old
    r = subprocess.run([sys.executable, "-m", "insteonrf.cli", "demod", "--demod", "c", str(iq)],
                       capture_output=True, text=True)
    pkts = [p for line in r.stdout.splitlines() if line[:1] in "01" for p in parse_bits(line)]
    assert any(p.cmd_name == "Ping" and p.crc_ok for p in pkts)


def test_clip_splits_bursts(tmp_path, monkeypatch):
    import numpy as np

    from insteonrf.dsp import modulate_fsk2
    from insteonrf.packet import Packet

    pkt = Packet.build("2B.93.07", "29.4E.52", cmd1=0x0F)
    samples = modulate_fsk2(pkt.to_bits())
    silence = np.zeros(20_000, dtype=np.int8)
    src = tmp_path / "stream.iq"
    np.concatenate([silence, samples, silence, samples, silence]).tofile(src)
    monkeypatch.chdir(tmp_path)
    rc, out, _ = run(cli.clip_main, ["-o", "b", str(src)])
    assert rc == 0 and out.count("samples") == 2
    assert sorted(p.name for p in tmp_path.glob("b-*.dat")) == ["b-0000.dat", "b-0001.dat"]


def test_recv_decode_with_file_backend():
    data = str(pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt")
    rc, out, _ = run(cli.recv_main, ["--backend", "file", "--replay", data, "-D"])
    assert rc == 0 and "crc" in out
    assert any("0D" in line for line in out.splitlines())


def test_recv_json_with_file_backend():
    import json

    data = str(pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt")
    rc, out, _ = run(cli.recv_main, ["--backend", "file", "--replay", data, "-D", "-j"])
    recs = [json.loads(line) for line in out.splitlines()]
    assert recs and all("raw" in r for r in recs)
    assert any(r["command"] == "Get Insteon Engine Version" for r in recs)


def test_monitor_writes_jsonl_and_dedupes(tmp_path):
    import json

    data = str(pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt")
    out_file = tmp_path / "rf.jsonl"
    rc, out, _ = run(cli.monitor_main, ["--backend", "file", "--replay", data,
                                        "-o", str(out_file), "--quiet"])
    assert rc == 0
    recs = [json.loads(line) for line in out_file.read_text().splitlines()]
    assert recs and all(r["repeats"] >= 1 for r in recs)
    # The capture holds hop repeats of each query, so dedupe must shrink it.
    raw_count = sum(1 for line in open(data) if line[:1] in "01")
    assert 0 < len(recs) < 40 and raw_count == 6


def test_monitor_no_dedupe_logs_every_repeat(tmp_path):
    data = str(pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt")
    a = tmp_path / "dedupe.jsonl"
    b = tmp_path / "raw.jsonl"
    run(cli.monitor_main, ["--backend", "file", "--replay", data, "-o", str(a), "--quiet"])
    run(cli.monitor_main, ["--backend", "file", "--replay", data, "-o", str(b), "--quiet",
                           "--no-dedupe"])
    assert len(b.read_text().splitlines()) > len(a.read_text().splitlines())


def test_send_dry_run_never_opens_a_radio():
    _, bits, _ = run(cli.pkt_main, ["-s", "2B.93.07", "-d", "29.4E.52", "0F", "00"])
    rc, out, err = run(cli.send_main, ["-n", "-v"], stdin=bits)
    assert rc == 0


def test_version_and_help():
    rc, out, _ = run(cli.main, ["--version"])
    assert rc == 0 and out.startswith("insteon-rf ")
    rc, out, _ = run(cli.main, ["--help"])
    assert "monitor" in out and "modulate" in out
    rc, _, err = run(cli.main, ["nope"])
    assert rc == 2


def test_stop_flag_ends_the_receive_loop(monkeypatch):
    """rflib swallows KeyboardInterrupt, so signals stop the loops via cli.STOP.

    A live radio's ``receive_bits`` never returns "no more data", so the loop
    has to notice the flag itself — here the second block sets it, as a signal
    would, and the loop must not read a third.
    """
    from insteonrf.radio import FileRadio

    data = pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt"

    class StopAfterTwo(FileRadio):
        reads = 0

        def receive_bits(self, timeout_ms=2000):
            self.reads += 1
            if self.reads == 2:
                cli.STOP.set()
            return super().receive_bits(timeout_ms)

    radio = StopAfterTwo(paths=[data], loop=True)  # endless, like a real radio
    monkeypatch.setattr(cli, "_open_radio", lambda *a, **k: radio)
    try:
        rc, out, _ = run(cli.recv_main, ["-D"])
        assert rc == 0 and radio.reads == 2
        assert 0 < len(out.splitlines()) < 12
    finally:
        cli.STOP.clear()


def test_signal_handlers_set_the_stop_flag():
    import signal

    cli._install_signal_handlers()
    try:
        assert not cli.STOP.is_set()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
        assert cli.STOP.is_set()
    finally:
        cli.STOP.clear()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_mqtt_credentials_come_from_the_environment(monkeypatch, tmp_path):
    """A password on the command line is visible in ps, so the env is the default."""
    seen = {}

    class FakePublisher:
        def __init__(self, host, port=1883, topic="insteon-rf", *, username=None,
                     password=None, **kw):
            seen.update(host=host, port=port, user=username, password=password)

        def publish(self, rec):
            pass

        def close(self):
            pass

    import insteonrf.monitor as mon

    monkeypatch.setattr(mon, "MqttPublisher", FakePublisher)
    monkeypatch.setenv("INSTEONRF_MQTT_USER", "from-env")
    monkeypatch.setenv("INSTEONRF_MQTT_PASS", "secret-from-env")
    data = str(pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt")
    rc, _, _ = run(cli.monitor_main, ["--backend", "file", "--replay", data,
                                      "--mqtt", "broker:1884", "--quiet"])
    assert rc == 0
    assert seen == {"host": "broker", "port": 1884, "user": "from-env",
                    "password": "secret-from-env"}


def test_broken_pipe_exits_quietly():
    """'insteon-rf recv | head' must not dump a traceback."""

    class ClosedPipe(io.StringIO):
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

    data = str(pathlib.Path(__file__).parent / "data" / "rfcat-get-engine.txt")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = ClosedPipe(), io.StringIO()
    try:
        rc = cli.main(["recv", "--backend", "file", "--replay", data, "-D"])
        err = sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    assert rc == 0 and "Traceback" not in err


def test_broken_pipe_still_releases_the_radio():
    """The exception has to unwind through the context manager first."""
    from insteonrf.radio import FileRadio

    class ClosedPipe(io.StringIO):
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

    radio = FileRadio(lines=["0" * 600])
    import insteonrf.cli as climod

    old_open, old_out = climod._open_radio, sys.stdout
    climod._open_radio = lambda *a, **k: radio
    sys.stdout = ClosedPipe()
    try:
        assert cli.main(["recv", "-D", "-a"]) == 0
    finally:
        climod._open_radio, sys.stdout = old_open, old_out
    assert radio.closed
