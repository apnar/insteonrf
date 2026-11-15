"""The display bookkeeping in the ESPHome component (heard.{h,cpp}), built and
run on the host. Skipped when there is no ``g++``."""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

COMPONENT = pathlib.Path(__file__).resolve().parent.parent / "esphome" / "components" / "insteon_rf"

PROGRAM = r"""
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include "heard.h"
using namespace esphome::insteon_rf;

static DecodedMessage frame(const char *hex) {
  DecodedMessage m{};
  m.frame_len = (uint8_t) (strlen(hex) / 2);
  for (size_t i = 0; i < m.frame_len; i++) sscanf(hex + 2 * i, "%2hhx", &m.frame[i]);
  m.copies = 2;
  return m;
}

int main() {
  char buf[32];

  // Field extraction and labels.
  HeardMessage on = heard_from(frame("0250294E522B93070F11FF"), -80.0f, 1000, 1794760000);
  assert(on.from == 0x294E52 && on.to == 0x2B9307 && on.cmd1 == 0x11);
  assert(on.kind() == MsgKind::DIRECT && on.hops_left == 3 && on.max_hops == 3 && !on.extended);
  assert(strcmp(command_label(on, buf, sizeof buf), "On") == 0);
  HeardMessage ack = heard_from(frame("0250294E522B9307270D02"), NAN, 1000, 0);
  assert(strcmp(command_label(ack, buf, sizeof buf), "ACK") == 0);
  HeardMessage leak = heard_from(frame("02503F539E000004CB1104"), -95.5f, 1000, 0);
  assert(leak.kind() == MsgKind::GROUP && leak.group() == 4);
  HeardMessage odd = heard_from(frame("0250294E522B93070F7700"), NAN, 1000, 0);
  assert(strcmp(command_label(odd, buf, sizeof buf), "cmd 77") == 0);
  format_addr(0x294E52, buf, sizeof buf);
  assert(strcmp(buf, "29.4E.52") == 0);

  // Log: newest first, bounded.
  HeardLog log;
  for (uint32_t i = 0; i < HeardLog::SIZE + 3; i++) {
    HeardMessage h = on;
    h.at_ms = i;
    log.add(h);
  }
  assert(log.size() == HeardLog::SIZE);
  assert(log.at(0).at_ms == HeardLog::SIZE + 2);
  assert(log.at(HeardLog::SIZE - 1).at_ms == 3);

  // Devices: counts, best/last RSSI, weakest first, activity window.
  HeardDevices dev;
  dev.add(on);                                 // 29.4E.52 at -80
  HeardMessage on2 = on; on2.rssi = -70.0f; on2.at_ms = 2000;
  dev.add(on2);
  dev.add(leak);                               // 3F.53.9E at -95.5
  dev.add(ack);                                // no RSSI: counts, keeps best
  assert(dev.size() == 2);
  assert(dev.at(0).count == 3 && dev.at(0).best_rssi == -70.0f && dev.at(0).last_rssi == -70.0f);
  size_t idx[4];
  size_t n = dev.weakest(2000, 60000, idx, 4);
  assert(n == 2 && dev.at(idx[0]).addr == 0x3F539E && dev.at(idx[1]).addr == 0x294E52);
  assert(dev.active(2000, 60000) == 2);
  assert(dev.active(2000 + 1500, 1000) == 0);
  // millis() wrapping past zero is still "recent".
  HeardMessage wrap = on; wrap.at_ms = 0xFFFFFF00u;
  HeardDevices dev2; dev2.add(wrap);
  assert(dev2.active(0x10, 1000) == 1);

  // Full table: the sender heard longest ago is replaced.
  HeardDevices full;
  for (uint32_t i = 0; i < HeardDevices::SIZE; i++) {
    HeardMessage h = on; h.from = 0x100000 + i; h.at_ms = 100 + i; full.add(h);
  }
  HeardMessage newer = on; newer.from = 0xABCDEF; newer.at_ms = 10000; full.add(newer);
  assert(full.size() == HeardDevices::SIZE);
  bool have_new = false, have_oldest = false;
  for (size_t i = 0; i < full.size(); i++) {
    have_new |= full.at(i).addr == 0xABCDEF;
    have_oldest |= full.at(i).addr == 0x100000;
  }
  assert(have_new && !have_oldest);

  // Names.
  DeviceNames names;
  names.add(0x294E52, "Master Bedroom Switch");
  names.label(0x294E52, buf, sizeof buf, 10);
  assert(strcmp(buf, "Master Bed") == 0);
  names.label(0x3F539E, buf, sizeof buf, 10);
  assert(strcmp(buf, "3F.53.9E") == 0);
  puts("ok");
  return 0;
}
"""


def test_heard_bookkeeping(tmp_path: pathlib.Path) -> None:
    cxx = shutil.which("g++")
    if cxx is None:
        pytest.skip("no g++ to build the ESPHome component with")
    src = tmp_path / "heard_test.cpp"
    src.write_text(PROGRAM)
    exe = tmp_path / "heard_test"
    subprocess.run(
        [cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(COMPONENT),
         str(src), str(COMPONENT / "heard.cpp"), str(COMPONENT / "decoder.cpp"), "-o", str(exe)],
        check=True,
    )
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    assert out.stdout.strip() == "ok"
