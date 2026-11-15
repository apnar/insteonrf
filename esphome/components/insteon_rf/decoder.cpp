// See decoder.h. Every step names the Python function it mirrors; keep them
// in step (tests/test_esphome_decoder.py holds them to identical output).

#include "decoder.h"

#include <cstring>

namespace esphome {
namespace insteon_rf {

namespace {

// packet.START_HEADER "1100111010101010" and its inverse, as 16-bit words
// read MSB first.
constexpr uint16_t START_HEADER = 0xCEAA;
constexpr uint16_t START_HEADER_INV = 0x3155;
constexpr size_t HEADER_BITS = 16;
constexpr size_t MARKER_OFFSET = 5;  // packet.MARKER_OFFSET
constexpr size_t FRAME_BITS = 28;    // '11' + 26 Manchester bits
constexpr size_t MIN_BYTES = 4;      // parse_bits(min_bytes=4)
// The most frames decode_frames can return from DECODER_MAX_BITS bits.
constexpr size_t MAX_FRAMES = DECODER_MAX_BITS / FRAME_BITS + 1;

constexpr uint8_t FLAG_BCAST = 0x80;
constexpr uint8_t FLAG_GROUP = 0x40;
constexpr uint8_t FLAG_EXT = 0x10;
constexpr uint8_t HOPS_MASK = 0x0C;
constexpr size_t STD_CRC_INDEX = 9;
constexpr size_t EXT_CRC_INDEX = 23;
constexpr size_t FLAGS_AT = 8;  // flags byte in the PLM frame

const uint8_t CRC_TABLE[16] = {0x00, 0x30, 0x60, 0x50, 0xC0, 0xF0, 0xA0, 0x90,
                               0x80, 0xB0, 0xE0, 0xD0, 0x40, 0x70, 0x20, 0x10};

// packet.pkt_crc
uint8_t pkt_crc(const uint8_t *data, size_t n) {
  uint8_t r = 0;
  for (size_t i = 0; i < n; i++) {
    r ^= data[i];
    r ^= CRC_TABLE[r & 0x0F];
  }
  return r;
}

}  // namespace

bool same_message(const DecodedMessage &a, const DecodedMessage &b) {
  if (a.frame_len != b.frame_len)
    return false;
  for (size_t i = 0; i < a.frame_len; i++) {
    const uint8_t mask = i == FLAGS_AT ? (uint8_t) ~HOPS_MASK : 0xFF;
    if ((a.frame[i] & mask) != (b.frame[i] & mask))
      return false;
  }
  return true;
}

void frame_hex(const DecodedMessage &m, char *out) {
  static const char HEX[] = "0123456789ABCDEF";
  for (size_t i = 0; i < m.frame_len; i++) {
    out[2 * i] = HEX[m.frame[i] >> 4];
    out[2 * i + 1] = HEX[m.frame[i] & 0x0F];
  }
  out[2 * m.frame_len] = '\0';
}

// --------------------------------------------------------------------- CaptureDecoder

uint16_t CaptureDecoder::word16_(size_t pos) const {
  uint16_t w = 0;
  for (size_t i = 0; i < HEADER_BITS; i++)
    w = (uint16_t) ((w << 1) | this->bits_[pos + i]);  // raw, not inverted
  return w;
}

// decode_frames from the marker after one header, then to_plm_bytes.
bool CaptureDecoder::decode_at_(size_t pos, DecodedMessage *msg) {
  uint8_t data[MAX_FRAMES];
  uint8_t idx[MAX_FRAMES];
  size_t nb = 0;

  // packet.decode_frames: stop at a bad marker, a Manchester error, a short
  // frame, or after index 0.
  size_t i = pos + MARKER_OFFSET;
  while (nb < MAX_FRAMES && i + 2 <= this->nbits_ && this->bit_(i) && this->bit_(i + 1)) {
    if (i + FRAME_BITS > this->nbits_)
      break;
    uint8_t index = 0, byte = 0;
    bool ok = true;
    for (size_t j = 0; j < 13; j++) {
      const uint8_t a = this->bit_(i + 2 + 2 * j);
      const uint8_t b = this->bit_(i + 3 + 2 * j);
      if (a == b) {
        ok = false;
        break;
      }
      // "01" is a 1 and "10" a 0: the second symbol is the bit. Both fields
      // are LSB first.
      if (j < 5)
        index |= (uint8_t) (b << j);
      else
        byte |= (uint8_t) (b << (j - 5));
    }
    if (!ok)
      break;
    idx[nb] = index;
    data[nb] = byte;
    nb++;
    i += FRAME_BITS;
    if (index == 0)
      break;
  }
  if (nb < MIN_BYTES)
    return false;
  this->packets_++;

  // packet.indexes_ok: the decoded counters are a prefix of 31, then 30..0
  // (extended) or 11..0 (standard).
  const bool extended = (data[0] & FLAG_EXT) != 0;
  const size_t want_len = extended ? 32 : 13;
  bool index_ok = nb <= want_len;
  for (size_t k = 0; index_ok && k < nb; k++) {
    const unsigned want = k == 0 ? 31u : (unsigned) ((extended ? 31 : 12) - k);
    index_ok = idx[k] == want;
  }

  // plm.to_plm_bytes(validate=True)
  const size_t crc_index = extended ? EXT_CRC_INDEX : STD_CRC_INDEX;
  const uint8_t hops_left = (data[0] >> 2) & 3;
  const uint8_t max_hops = data[0] & 3;
  if (nb < crc_index + 1 || !index_ok || hops_left > max_hops ||
      data[crc_index] != pkt_crc(data, crc_index)) {
    this->rejected_++;
    return false;
  }

  // RF slot 1 is the destination, slot 2 the sender -- except for a group
  // broadcast, which leads with the sender. Wire addresses are low byte
  // first; the PLM frame wants from, to, each high byte first.
  const bool group_bcast = (data[0] & (FLAG_BCAST | FLAG_GROUP)) == (FLAG_BCAST | FLAG_GROUP);
  const uint8_t *from_wire = group_bcast ? &data[1] : &data[4];
  const uint8_t *to_wire = group_bcast ? &data[4] : &data[1];
  uint8_t *f = msg->frame;
  f[0] = 0x02;
  f[1] = extended ? 0x51 : 0x50;
  f[2] = from_wire[2];
  f[3] = from_wire[1];
  f[4] = from_wire[0];
  f[5] = to_wire[2];
  f[6] = to_wire[1];
  f[7] = to_wire[0];
  f[8] = data[0];
  f[9] = data[7];
  f[10] = data[8];
  if (extended)
    std::memcpy(&f[11], &data[9], 14);  // RF d1..d13 + data CRC = PLM d1..d14
  msg->frame_len = extended ? PLM_EXT_FRAME_LEN : PLM_STD_FRAME_LEN;
  msg->hops_left = hops_left;
  msg->copies = 1;
  msg->bit_pos = (uint16_t) pos;
  return true;
}

size_t CaptureDecoder::decode_copies(const uint8_t *buf, size_t len, uint32_t sync_word,
                                     DecodedMessage *out, size_t max_out) {
  this->headers_ = this->packets_ = this->rejected_ = this->overflow_ = 0;
  this->invert_ = 0;
  if (len > DECODER_MAX_CAPTURE_BYTES)
    len = DECODER_MAX_CAPTURE_BYTES;

  // radio.mqtt.with_sync_header: the header in the polarity the radio
  // matched, unless the capture already starts with one.
  size_t n = 0;
  uint16_t lead = 0;
  for (size_t i = 0; i < HEADER_BITS && i < len * 8; i++)
    lead = (uint16_t) ((lead << 1) | ((buf[i >> 3] >> (7 - (i & 7))) & 1));
  const bool has_header = len * 8 >= HEADER_BITS && (lead == START_HEADER || lead == START_HEADER_INV);
  if (!has_header) {
    const uint16_t header = (uint16_t) (sync_word & 0xFFFF);
    for (size_t i = 0; i < HEADER_BITS; i++)
      this->bits_[n++] = (header >> (15 - i)) & 1;
  }
  for (size_t i = 0; i < len * 8; i++)
    this->bits_[n++] = (buf[i >> 3] >> (7 - (i & 7))) & 1;
  this->nbits_ = n;
  if (n < HEADER_BITS)
    return 0;

  // packet.find_headers: whichever polarity of the start header occurs
  // first decides; the whole string is inverted if it is the inverse.
  const size_t last = n - HEADER_BITS;
  size_t first = 0;
  bool found = false;
  for (size_t p = 0; p <= last; p++) {
    const uint16_t w = this->word16_(p);
    if (w == START_HEADER || w == START_HEADER_INV) {
      first = p;
      this->invert_ = w == START_HEADER_INV ? 1 : 0;
      found = true;
      break;
    }
  }
  if (!found)
    return 0;

  // Then every non-overlapping START_HEADER in the normalised string.
  const uint16_t target = this->invert_ ? START_HEADER_INV : START_HEADER;
  size_t count = 0;
  for (size_t p = first; p <= last;) {
    if (this->word16_(p) != target) {
      p++;
      continue;
    }
    this->headers_++;
    DecodedMessage m;
    if (this->decode_at_(p, &m)) {
      if (count < max_out)
        out[count++] = m;
      else
        this->overflow_++;
    }
    p += HEADER_BITS;
  }
  return count;
}

size_t CaptureDecoder::decode(const uint8_t *buf, size_t len, uint32_t sync_word, DecodedMessage *out,
                              size_t max_out) {
  DecodedMessage copies[DECODER_MAX_COPIES];
  const size_t n = this->decode_copies(buf, len, sync_word, copies, DECODER_MAX_COPIES);
  // listener.Folder within one capture: every copy has the capture's time,
  // so only the key matters; keep the copy with the most hops left.
  size_t count = 0;
  for (size_t i = 0; i < n; i++) {
    size_t k = 0;
    while (k < count && !same_message(out[k], copies[i]))
      k++;
    if (k < count) {
      out[k].copies++;
      if (copies[i].hops_left > out[k].hops_left) {
        std::memcpy(out[k].frame, copies[i].frame, sizeof(out[k].frame));
        out[k].hops_left = copies[i].hops_left;
      }
    } else if (count < max_out) {
      out[count++] = copies[i];
    } else {
      this->overflow_++;
    }
  }
  return count;
}

// --------------------------------------------------------------------- MessageFolder

bool MessageFolder::add(const DecodedMessage &m, int64_t t_ms, float rssi, FoldedMessage *evicted) {
  Slot *free_slot = nullptr;
  Slot *oldest = nullptr;
  for (Slot &s : this->slots_) {
    if (!s.used) {
      if (free_slot == nullptr)
        free_slot = &s;
      continue;
    }
    if (oldest == nullptr || s.m.t_ms < oldest->m.t_ms)
      oldest = &s;
    if (!same_message(s.m.msg, m))
      continue;
    FoldedMessage &cur = s.m;
    if (t_ms - cur.t_ms > (int64_t) this->window_ms_) {
      // Same bytes, but too late to be a hop copy: a retry. The old one
      // leaves now and the retry takes its place.
      *evicted = cur;
      cur.msg = m;
      cur.t_ms = t_ms;
      cur.rssi = rssi;
      return true;
    }
    cur.msg.copies = (uint16_t) (cur.msg.copies + m.copies);
    // listener.Folder keeps (least repaired, most hops left); nothing is
    // repaired here, so the most hops left wins, ties keep the first.
    if (m.hops_left > cur.msg.hops_left) {
      std::memcpy(cur.msg.frame, m.frame, sizeof(cur.msg.frame));
      cur.msg.hops_left = m.hops_left;
    }
    if (t_ms < cur.t_ms) {
      cur.t_ms = t_ms;
      cur.msg.bit_pos = m.bit_pos;
    }
    // _best(): the larger, a NAN on either side is "none".
    if (!(rssi != rssi) && (cur.rssi != cur.rssi || rssi > cur.rssi))
      cur.rssi = rssi;
    return false;
  }
  bool out = false;
  if (free_slot == nullptr) {
    // Table full (more than SLOTS distinct transmissions in one window, which
    // Insteon's slot grid cannot produce): release the oldest early.
    *evicted = oldest->m;
    free_slot = oldest;
    out = true;
  }
  free_slot->used = true;
  free_slot->m.msg = m;
  free_slot->m.t_ms = t_ms;
  free_slot->m.rssi = rssi;
  return out;
}

bool MessageFolder::pop_ready(int64_t now_ms, FoldedMessage *out) {
  Slot *best = nullptr;
  for (Slot &s : this->slots_) {
    if (!s.used || now_ms - s.m.t_ms <= (int64_t) this->window_ms_)
      continue;
    if (best == nullptr || s.m.t_ms < best->m.t_ms)
      best = &s;
  }
  if (best == nullptr)
    return false;
  *out = best->m;
  best->used = false;
  return true;
}

size_t MessageFolder::pending() const {
  size_t n = 0;
  for (const Slot &s : this->slots_)
    n += s.used ? 1 : 0;
  return n;
}

}  // namespace insteon_rf
}  // namespace esphome
