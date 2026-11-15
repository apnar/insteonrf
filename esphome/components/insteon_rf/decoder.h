// Insteon RF capture decoder: a port of the Python reference, with no
// ESPHome or Arduino dependency so the host can compile and test it.
//
// Reference (insteonrf/listener.py, contract v1, Doc/REARCHITECTURE.md):
//
//   capture_messages(bits, rx, t, sync_word=..)
//     = with_sync_header   put back the 16-bit start header the radio consumed
//     + parse_bits         start headers in either polarity, 28-bit frames
//     + to_plm_bytes(validate=True)
//                          frame-index counters, enough bytes, hops_left <=
//                          max_hops, packet CRC; then the PLM frame layout
//     + Folder             hop copies -> one message per transmission
//
// There is no repair here: a copy either decodes clean or it is dropped
// (`repaired` is always 0 for this receiver). The extended data CRC is not
// checked, as in the reference: the packet CRC covers every data byte.
//
// tools/insteon_rf_decode.cpp wraps this in a CLI and tests/test_esphome_decoder.py
// runs it over tests/data/contract-v1/*-vectors.jsonl against the Python
// output -- that is the acceptance test, so change both together.

#pragma once

#include <cstddef>
#include <cstdint>

namespace esphome {
namespace insteon_rf {

/// Longest capture the decoder accepts (the YAML caps capture_bytes at 255).
static constexpr size_t DECODER_MAX_CAPTURE_BYTES = 255;
/// Bits held: the capture plus the 16-bit start header put back in front.
static constexpr size_t DECODER_MAX_BITS = DECODER_MAX_CAPTURE_BYTES * 8 + 16;
/// Valid copies kept per capture. A 255-byte capture holds five hop slots at
/// most; anything beyond this is counted in CaptureDecoder::overflow().
static constexpr size_t DECODER_MAX_COPIES = 16;
/// PLM frame lengths, `02 50 ...` and `02 51 ...`.
static constexpr uint8_t PLM_STD_FRAME_LEN = 11;
static constexpr uint8_t PLM_EXT_FRAME_LEN = 25;
/// On-air bit rate, for turning a bit offset into time.
static constexpr uint32_t DECODER_BITRATE = 9124;

/// One decoded transmission (or, from decode_copies(), one hop copy of it).
struct DecodedMessage {
  /// The PLM inbound frame (02 50 + 9 bytes, or 02 51 + 23 bytes).
  uint8_t frame[PLM_EXT_FRAME_LEN];
  uint8_t frame_len;
  /// Hops left of the copy whose frame is kept (the highest seen).
  uint8_t hops_left;
  /// Hop copies folded into this message.
  uint16_t copies;
  /// Start-header offset of the first copy, in bits from the start of the
  /// capture *as parsed*, i.e. with the start header put back: the first
  /// copy of a FIFO dump sits at 0. Divide by DECODER_BITRATE for time.
  uint16_t bit_pos;
};

/// Same transmission: identical frames except the hops-left bits (0x0C) of
/// the flags byte. The PLM frame is a rearrangement of exactly the bytes
/// plm.message_key() compares, so this is the same key.
bool same_message(const DecodedMessage &a, const DecodedMessage &b);

/// Render `frame` as upper-case hex into `out` (needs 2 * frame_len + 1).
void frame_hex(const DecodedMessage &m, char *out);

class CaptureDecoder {
 public:
  /// Every valid copy in one capture, in the order they occur.
  ///
  /// `buf`/`len` is the FIFO content, starting with the first bit after the
  /// sync word; `sync_word`'s low 16 bits are the start header the radio
  /// matched on (in the polarity it matched), and are put back in front
  /// unless the capture already starts with a header. Returns the number of
  /// copies written to `out` (at most `max_out`); each has copies == 1.
  size_t decode_copies(const uint8_t *buf, size_t len, uint32_t sync_word, DecodedMessage *out,
                       size_t max_out);

  /// decode_copies() then fold hop copies within the capture: exactly
  /// listener.capture_messages() minus the fields this receiver does not
  /// have. Order is first appearance.
  size_t decode(const uint8_t *buf, size_t len, uint32_t sync_word, DecodedMessage *out, size_t max_out);

  /// Start headers found in the last capture, packets of >= 4 bytes among
  /// them, and those that failed validation; copies dropped for want of room.
  uint32_t headers() const { return this->headers_; }
  uint32_t packets() const { return this->packets_; }
  uint32_t rejected() const { return this->rejected_; }
  uint32_t overflow() const { return this->overflow_; }

 protected:
  inline uint8_t bit_(size_t i) const { return this->bits_[i] ^ this->invert_; }
  uint16_t word16_(size_t pos) const;
  bool decode_at_(size_t pos, DecodedMessage *msg);

  uint8_t bits_[DECODER_MAX_BITS];
  size_t nbits_{0};
  uint8_t invert_{0};
  uint32_t headers_{0};
  uint32_t packets_{0};
  uint32_t rejected_{0};
  uint32_t overflow_{0};
};

/// One message waiting in (or released from) a MessageFolder.
struct FoldedMessage {
  DecodedMessage msg;
  /// On-air time of the earliest copy, ms, in whatever clock the caller uses.
  int64_t t_ms;
  /// Strongest copy, dBm; NAN when unknown.
  float rssi;
};

/// Hop copies -> one message per transmission, across captures: listener.Folder
/// with millisecond times. A transmission whose copies straddle two captures is
/// therefore released once. Fixed storage, no heap.
class MessageFolder {
 public:
  static constexpr size_t SLOTS = 16;
  explicit MessageFolder(uint32_t window_ms = 500) : window_ms_(window_ms) {}

  void set_window_ms(uint32_t ms) { this->window_ms_ = ms; }
  uint32_t window_ms() const { return this->window_ms_; }

  /// Fold one copy (or an already folded message: its copies add up) in.
  ///
  /// Returns true and fills `evicted` when a message has to leave at once:
  /// a retry of the same bytes later than the window releases the old one
  /// (listener.Folder does the same), and a full table releases the oldest.
  /// Publish it straight away.
  bool add(const DecodedMessage &m, int64_t t_ms, float rssi, FoldedMessage *evicted);

  /// The oldest message whose window (from its first copy) has closed by
  /// `now_ms`; false when there is none. Call until false.
  bool pop_ready(int64_t now_ms, FoldedMessage *out);

  size_t pending() const;

 protected:
  struct Slot {
    FoldedMessage m;
    bool used;
  };
  Slot slots_[SLOTS]{};
  uint32_t window_ms_;
};

}  // namespace insteon_rf
}  // namespace esphome
