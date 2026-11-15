#include "heard.h"

#include <cmath>
#include <cstdio>
#include <cstring>

namespace esphome {
namespace insteon_rf {

int HeardMessage::group() const {
  const MsgKind k = this->kind();
  if (k == MsgKind::GROUP)
    return (int) (this->to & 0xFF);
  if (k == MsgKind::CLEANUP)
    return this->cmd2;  // a cleanup carries the group in cmd2
  return -1;
}

HeardMessage heard_from(const DecodedMessage &m, float rssi, uint32_t at_ms, int64_t epoch_s) {
  // PLM frame: 02 50|51, from(3), to(3), flags, cmd1, cmd2 [, d1..d14].
  const uint8_t *f = m.frame;
  HeardMessage h{};
  h.from = ((uint32_t) f[2] << 16) | ((uint32_t) f[3] << 8) | f[4];
  h.to = ((uint32_t) f[5] << 16) | ((uint32_t) f[6] << 8) | f[7];
  h.flags = f[8];
  h.cmd1 = f[9];
  h.cmd2 = f[10];
  h.hops_left = (h.flags >> 2) & 3;
  h.max_hops = h.flags & 3;
  h.copies = m.copies;
  h.extended = f[1] == 0x51;
  h.rssi = rssi;
  h.at_ms = at_ms;
  h.epoch_s = epoch_s;
  return h;
}

namespace {
struct CmdName {
  uint8_t cmd1;
  const char *name;
};
// Standard-message commands as they appear on this network (insteonrf
// cmds.py, measured against five months of insteon-mqtt logs). Kept short:
// the screen is 320 px wide.
const CmdName CMD_NAMES[] = {
    {0x01, "Set btn"},  {0x02, "Set btn"},   {0x03, "Product"},  {0x06, "Cleanup"},
    {0x0D, "Engine"},   {0x0F, "Ping"},      {0x10, "ID req"},   {0x11, "On"},
    {0x12, "Fast On"},  {0x13, "Off"},       {0x14, "Fast Off"}, {0x15, "Bright"},
    {0x16, "Dim"},      {0x17, "Start dim"}, {0x18, "Stop dim"}, {0x19, "Status"},
    {0x1F, "Get flags"}, {0x20, "Set flags"}, {0x21, "Light lvl"}, {0x2E, "Ext set"},
    {0x2F, "ALDB"},     {0x30, "Beep"},
};
}  // namespace

const char *command_label(const HeardMessage &m, char *buf, size_t len) {
  switch (m.kind()) {
    case MsgKind::ACK:
    case MsgKind::CLEANUP_ACK:
      return "ACK";
    case MsgKind::NAK:
    case MsgKind::CLEANUP_NAK:
      return "NAK";
    default:
      break;
  }
  for (const auto &c : CMD_NAMES) {
    if (c.cmd1 == m.cmd1)
      return c.name;
  }
  snprintf(buf, len, "cmd %02X", m.cmd1);
  return buf;
}

void format_addr(uint32_t addr, char *buf, size_t len) {
  snprintf(buf, len, "%02X.%02X.%02X", (unsigned) ((addr >> 16) & 0xFF), (unsigned) ((addr >> 8) & 0xFF),
           (unsigned) (addr & 0xFF));
}

void HeardLog::add(const HeardMessage &m) {
  this->head_ = (this->head_ + 1) % SIZE;
  this->ring_[this->head_] = m;
  if (this->count_ < SIZE)
    this->count_++;
}

const HeardMessage &HeardLog::at(size_t i) const {
  // Callers stay below size(); clamp rather than read stale slots if not.
  if (i >= this->count_)
    i = this->count_ ? this->count_ - 1 : 0;
  return this->ring_[(this->head_ + SIZE - i) % SIZE];
}

void HeardDevices::add(const HeardMessage &m) {
  size_t i = 0;
  for (; i < this->count_; i++) {
    if (this->table_[i].addr == m.from)
      break;
  }
  if (i == this->count_) {
    if (this->count_ < SIZE) {
      this->count_++;
    } else {
      // Replace the sender heard longest ago (wrap-safe age comparison).
      size_t oldest = 0;
      for (size_t j = 1; j < SIZE; j++) {
        if ((uint32_t) (m.at_ms - this->table_[j].last_ms) > (uint32_t) (m.at_ms - this->table_[oldest].last_ms))
          oldest = j;
      }
      i = oldest;
    }
    this->table_[i] = Entry{m.from, 0, NAN, NAN, m.at_ms};
  }
  Entry &e = this->table_[i];
  e.count++;
  e.last_ms = m.at_ms;
  if (!std::isnan(m.rssi)) {
    e.last_rssi = m.rssi;
    if (std::isnan(e.best_rssi) || m.rssi > e.best_rssi)
      e.best_rssi = m.rssi;
  }
}

size_t HeardDevices::active(uint32_t now_ms, uint32_t window_ms) const {
  size_t n = 0;
  for (size_t i = 0; i < this->count_; i++) {
    if ((uint32_t) (now_ms - this->table_[i].last_ms) <= window_ms)
      n++;
  }
  return n;
}

size_t HeardDevices::weakest(uint32_t now_ms, uint32_t window_ms, size_t *out, size_t n) const {
  size_t cand[SIZE];
  size_t m = 0;
  for (size_t i = 0; i < this->count_; i++) {
    if ((uint32_t) (now_ms - this->table_[i].last_ms) <= window_ms)
      cand[m++] = i;
  }
  // Weakest best RSSI first; senders with no RSSI at all go last.
  auto before = [this](size_t a, size_t b) {
    const float x = this->table_[a].best_rssi, y = this->table_[b].best_rssi;
    if (std::isnan(x) != std::isnan(y))
      return !std::isnan(x);
    return !std::isnan(x) && x < y;
  };
  const size_t k = n < m ? n : m;
  for (size_t s = 0; s < k; s++) {
    size_t best = s;
    for (size_t j = s + 1; j < m; j++) {
      if (before(cand[j], cand[best]))
        best = j;
    }
    const size_t t = cand[s];
    cand[s] = cand[best];
    cand[best] = t;
    out[s] = cand[s];
  }
  return k;
}

const char *DeviceNames::find(uint32_t addr) const {
  for (const auto &p : this->names_) {
    if (p.first == addr)
      return p.second.c_str();
  }
  return nullptr;
}

void DeviceNames::label(uint32_t addr, char *buf, size_t len, size_t max_chars) const {
  if (len == 0)
    return;
  const char *name = this->find(addr);
  if (name == nullptr) {
    format_addr(addr, buf, len);
    return;
  }
  size_t n = strlen(name);
  if (n > max_chars)
    n = max_chars;
  if (n >= len)
    n = len - 1;
  memcpy(buf, name, n);
  buf[n] = '\0';
}

}  // namespace insteon_rf
}  // namespace esphome
