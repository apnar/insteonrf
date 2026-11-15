#pragma once

// What this listener has heard, for the board's own display: the last few
// decoded messages and a per-device table (count, best and last RSSI), plus
// human labels for commands and an optional address -> name map.
//
// Portable C++ with no ESPHome dependencies, like decoder.h, so the host
// harness can test it. Everything runs on ESPHome's main loop (the decoder
// releases messages there and display lambdas run there), so no locking.

#include <cstddef>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "decoder.h"

namespace esphome {
namespace insteon_rf {

/// Insteon message type, from the top three bits of the flags byte.
enum class MsgKind : uint8_t {
  DIRECT = 0,
  ACK = 1,
  CLEANUP = 2,
  CLEANUP_ACK = 3,
  BROADCAST = 4,
  NAK = 5,
  GROUP = 6,
  CLEANUP_NAK = 7,
};

/// One decoded message, reduced to what a screen shows.
struct HeardMessage {
  uint32_t from;      // 0xAABBCC
  uint32_t to;        // destination, or for group broadcasts 0x0000GG
  uint8_t flags;
  uint8_t cmd1;
  uint8_t cmd2;
  uint8_t hops_left;
  uint8_t max_hops;
  uint16_t copies;
  bool extended;
  float rssi;         // NAN when unknown
  uint32_t at_ms;     // monotonic ms when it was released
  int64_t epoch_s;    // wall clock seconds, 0 if the clock was not set

  MsgKind kind() const { return static_cast<MsgKind>(this->flags >> 5); }
  /// Group number for a group broadcast or cleanup, else -1.
  int group() const;
};

/// Fill a HeardMessage from a decoded PLM frame.
HeardMessage heard_from(const DecodedMessage &m, float rssi, uint32_t at_ms, int64_t epoch_s);

/// "On", "Fast Off", "ACK", "Cleanup", ... or "cmd 2F". Never null.
/// `buf` (>= 12 bytes) is used only for the fallback.
const char *command_label(const HeardMessage &m, char *buf, size_t len);

/// "29.4E.52"; `buf` needs 9 bytes.
void format_addr(uint32_t addr, char *buf, size_t len);

/// Last N messages, newest first.
class HeardLog {
 public:
  static constexpr size_t SIZE = 12;
  void add(const HeardMessage &m);
  size_t size() const { return this->count_; }
  /// i = 0 is the newest.
  const HeardMessage &at(size_t i) const;

 protected:
  HeardMessage ring_[SIZE]{};
  size_t head_{0};
  size_t count_{0};
};

/// Per-sender table. Full table: the least recently heard sender is replaced.
class HeardDevices {
 public:
  static constexpr size_t SIZE = 96;
  struct Entry {
    uint32_t addr;
    uint32_t count;
    float best_rssi;   // NAN until a copy with RSSI is heard
    float last_rssi;
    uint32_t last_ms;
  };
  void add(const HeardMessage &m);
  size_t size() const { return this->count_; }
  const Entry &at(size_t i) const { return this->table_[i]; }
  /// Senders heard within `window_ms` of `now_ms`.
  size_t active(uint32_t now_ms, uint32_t window_ms) const;
  /// Up to `n` indexes of senders heard within the window, weakest best RSSI
  /// first (unknown RSSI last). Returns how many were written.
  size_t weakest(uint32_t now_ms, uint32_t window_ms, size_t *out, size_t n) const;

 protected:
  Entry table_[SIZE]{};
  size_t count_{0};
};

/// Optional address -> short name, from YAML.
class DeviceNames {
 public:
  void add(uint32_t addr, const std::string &name) { this->names_.emplace_back(addr, name); }
  /// The name, or nullptr.
  const char *find(uint32_t addr) const;
  /// Name if known, else the dotted address, truncated to `max_chars`.
  void label(uint32_t addr, char *buf, size_t len, size_t max_chars) const;
  size_t size() const { return this->names_.size(); }

 protected:
  std::vector<std::pair<uint32_t, std::string>> names_;
};

}  // namespace insteon_rf
}  // namespace esphome
