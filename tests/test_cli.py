import io
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
