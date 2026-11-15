#include "insteon_rf.h"
#include "esphome/core/application.h"
#include "esphome/core/log.h"

#ifdef USE_MQTT
#include "esphome/components/mqtt/mqtt_client.h"
#endif

#include <cstdio>
#include <cstring>
#include <ctime>
#include <sys/time.h>

namespace esphome {
namespace insteon_rf {

static const char *const TAG = "insteon_rf";

// How many captures to describe at INFO after boot.
static const uint8_t BRINGUP_CAPTURES = 10;
// ... and how many decoded messages.
static const uint8_t BRINGUP_DECODED = 10;
// Before SNTP has set the clock there is no epoch time to put in "t"; a
// decoded message is dropped (and counted) rather than dated 1970.
static const time_t CLOCK_VALID_AFTER = 1700000000;  // 2023-11

// --------------------------------------------------------------------- the gate

bool InsteonRF::manchester_gate_ok_(const uint8_t *buf, size_t len) const {
  const size_t nbits = len * 8;
  auto bit = [buf](size_t i) -> uint8_t { return (buf[i >> 3] >> (7 - (i & 7))) & 1; };
  // Roughly 26 of every 28 on-air bits are Manchester pairs, and a valid pair
  // is only 01 or 10 -- never 00 or 11. Random noise fails within a handful
  // of bits, which is what makes this cheap check enough to run with the
  // preamble detector off in a crowded 915 MHz band.
  //
  // Layout after the sync word. Every radio here syncs on a word that ends
  // with the 16-bit start header, which swallows the preamble, the literal
  // '11' frame marker and the first 9 bits of the Manchester-coded frame
  // index 31, so the FIFO starts one bit into that index:
  //
  //   bit 0        the last bit of Manchester(index 31)
  //   bits 1..16   Manchester(data byte 0), 8 pairs
  //   bit 17..     frame 2: the '11' marker, then 5 index pairs and 8 data
  //                pairs; 28 bits per frame thereafter.
  //
  // Nothing here depends on the bit polarity, deliberately. A Manchester pair
  // differs in either polarity, and the marker is two *equal* bits whether it
  // arrives as 11 or 00. So if the sync word has to be flipped to the other
  // polarity on real hardware, the gate keeps working unchanged. (Bit 0 is
  // a single polarity-dependent bit and is not worth checking.)
  if (nbits < 17)
    return false;
  // Count violations rather than failing on the first. Noise breaks about
  // half of the ~50 pairs checked here, so allowing a couple costs nothing
  // in selectivity (a noise capture passes with <= 2 about once in 10^12),
  // while a real packet with one or two bad symbols in its first frames is
  // exactly what the host repairs from hard bits: 94% of one-symbol and 85%
  // of two-symbol errors. Measured 2026-09-23 the strict gate rejected 3 of
  // 15 captures under test traffic -- the weak ones, which the dongle's
  // captures (no gate at all) still delivered.
  uint8_t bad = 0;
  for (uint8_t j = 0; j < 8; j++) {
    const size_t at = 1 + 2 * j;
    if (bit(at) == bit(at + 1) && ++bad > this->manchester_gate_errors_)
      return false;
  }
  for (uint8_t k = 0; k + 1 < this->manchester_gate_; k++) {
    const size_t base = 17 + (size_t) k * 28;
    if (base + 28 > nbits)
      break;
    if (bit(base) != bit(base + 1) && ++bad > this->manchester_gate_errors_)
      return false;  // the frame marker: two equal bits
    for (uint8_t j = 0; j < 13; j++) {
      const size_t at = base + 2 + 2 * j;
      if (bit(at) == bit(at + 1) && ++bad > this->manchester_gate_errors_)
        return false;
    }
  }
  return true;
}

// --------------------------------------------------------------------- counters

void InsteonRF::tick_minute_() {
  const uint32_t now = millis();
  if (now - this->last_minute_mark_ < 60000)
    return;
  // Kept for the display: the previous whole minute, not a rolling window.
  this->captures_last_minute_ = this->captures_ - this->captures_at_mark_;
  this->accepted_last_minute_ = this->accepted_ - this->accepted_at_mark_;
  this->lost_last_minute_ = this->lost_ - this->lost_at_mark_;
  this->decoded_last_minute_ = this->decoded_ - this->decoded_at_mark_;
  this->freq_offset_last_minute_ =
      this->freq_offset_n_ ? this->freq_offset_sum_ / (float) this->freq_offset_n_ : NAN;
#ifdef USE_SENSOR
  if (this->captures_sensor_ != nullptr)
    this->captures_sensor_->publish_state(this->captures_last_minute_);
  if (this->accepted_sensor_ != nullptr)
    this->accepted_sensor_->publish_state(this->accepted_last_minute_);
  if (this->lost_sensor_ != nullptr)
    this->lost_sensor_->publish_state(this->lost_last_minute_);
  if (this->decoded_sensor_ != nullptr)
    this->decoded_sensor_->publish_state(this->decoded_last_minute_);
  // A minute with no packets says nothing about the offset; keep the last
  // value rather than publishing a gap.
  if (this->frequency_offset_sensor_ != nullptr && this->freq_offset_n_)
    this->frequency_offset_sensor_->publish_state(this->freq_offset_last_minute_);
#endif
  if (this->lost_last_minute_)
    ESP_LOGW(TAG, "%u capture(s) lost by the radio in the last minute",
             (unsigned) this->lost_last_minute_);
  this->captures_at_mark_ = this->captures_;
  this->accepted_at_mark_ = this->accepted_;
  this->lost_at_mark_ = this->lost_;
  this->decoded_at_mark_ = this->decoded_;
  if (this->undated_) {
    ESP_LOGW(TAG, "%u decoded message(s) dropped: the clock is not set yet (SNTP)", (unsigned) this->undated_);
    this->undated_ = 0;
  }
  this->freq_offset_sum_ = 0.0f;
  this->freq_offset_n_ = 0;
  this->last_minute_mark_ = now;
}

// --------------------------------------------------------------------- one capture

void InsteonRF::process_capture_(const uint8_t *buf, size_t len, float rssi, uint32_t first_bit_us,
                                 float offset_khz) {
  this->captures_++;
  this->last_rssi_ = rssi;
#ifdef USE_SENSOR
  if (this->last_rssi_sensor_ != nullptr)
    this->last_rssi_sensor_->publish_state(rssi);
#endif

  const bool loud_enough = rssi >= this->rssi_floor_;
  const bool gate_ok = this->manchester_gate_ok_(buf, len);

  // The first few captures after boot are the bring-up story: are captures
  // arriving at all (sync word and wiring), do they pass the gate (polarity
  // and framing), and what do the leading bytes look like.
  if (this->bringup_logged_ < BRINGUP_CAPTURES && len >= 8) {
    this->bringup_logged_++;
    ESP_LOGI(TAG,
             "capture %u/%u: %u bytes at %.1f dBm, offset %.1f kHz, gate %s, "
             "starts %02X %02X %02X %02X %02X %02X %02X %02X",
             (unsigned) this->bringup_logged_, (unsigned) BRINGUP_CAPTURES, (unsigned) len, rssi,
             offset_khz, gate_ok ? "PASS" : "fail", buf[0], buf[1], buf[2], buf[3], buf[4], buf[5],
             buf[6], buf[7]);
  }

  if (!loud_enough) {
    ESP_LOGD(TAG, "dropping capture at %.1f dBm (floor %.1f)", rssi, this->rssi_floor_);
    return;
  }
  if (!gate_ok) {
    ESP_LOGD(TAG, "capture failed the Manchester gate");
    return;
  }

  this->accepted_++;
  this->seq_++;
  this->last_accepted_rssi_ = rssi;
  this->last_accepted_ms_ = millis();
  if (!std::isnan(offset_khz)) {
    this->freq_offset_sum_ += offset_khz;
    this->freq_offset_n_++;
  }
  if (this->publish_captures_())
    this->publish_capture_(buf, len, rssi, first_bit_us);
  if (this->publish_decoded_enabled_())
    this->decode_capture_(buf, len, rssi, first_bit_us);
}

// --------------------------------------------------------------------- decoded

int64_t InsteonRF::now_us64_() {
  const uint32_t m = micros();
  if (m < this->last_micros_)
    this->micros_high_ += (int64_t) 1 << 32;
  this->last_micros_ = m;
  return this->micros_high_ + m;
}

void InsteonRF::decode_capture_(const uint8_t *buf, size_t len, float rssi, uint32_t first_bit_us) {
  // Every copy is dated at its own start header, on the board's monotonic
  // clock: the capture's first FIFO bit plus the header's offset at 9124
  // baud (the host pods' packet_air_times; a 162-byte capture spans three
  // hop slots). It becomes epoch time only when published.
  const int64_t now_us = this->now_us64_();
  const int64_t first_us = now_us - (int64_t) (uint32_t) (this->last_micros_ - first_bit_us);
  DecodedMessage copies[DECODER_MAX_COPIES];
  const size_t n = this->decoder_.decode_copies(buf, len, this->sync_word_, copies, DECODER_MAX_COPIES);
  ESP_LOGV(TAG, "decoded %u copies (%u headers, %u packets, %u rejected)", (unsigned) n,
           (unsigned) this->decoder_.headers(), (unsigned) this->decoder_.packets(),
           (unsigned) this->decoder_.rejected());
  for (size_t i = 0; i < n; i++) {
    // bit_pos counts the 16-bit start header put back in front, so the first
    // copy lands on the capture's own stamp, as on the host.
    const int64_t t_ms = (first_us + (int64_t) copies[i].bit_pos * 1000000LL / DECODER_BITRATE) / 1000;
    FoldedMessage evicted;
    if (this->folder_.add(copies[i], t_ms, rssi, &evicted))
      this->release_decoded_(evicted, now_us / 1000);
  }
}

void InsteonRF::service_decoded_() {
  if (!this->publish_decoded_enabled_())
    return;
  const int64_t now_ms = this->now_us64_() / 1000;
  FoldedMessage m;
  while (this->folder_.pop_ready(now_ms - RELEASE_LATENESS_MS, &m))
    this->release_decoded_(m, now_ms);
}

void InsteonRF::release_decoded_(const FoldedMessage &m, int64_t now_ms) {
  // For the display: recorded whether or not the clock is set yet, and for
  // every release (window closed, a retry, or a full fold table).
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  const HeardMessage h = heard_from(m.msg, m.rssi, millis(), tv.tv_sec >= CLOCK_VALID_AFTER ? tv.tv_sec : 0);
  this->heard_log_.add(h);
  this->heard_devices_.add(h);
  this->publish_decoded_(m, now_ms);
}

void InsteonRF::publish_decoded_(const FoldedMessage &m, int64_t now_ms) {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (tv.tv_sec < CLOCK_VALID_AFTER) {
    this->undated_++;
    return;
  }
  this->decoded_++;
  // Epoch ms of the first copy's start header: the wall clock now, less how
  // long ago that was on the monotonic clock.
  const int64_t epoch_now_ms = (int64_t) tv.tv_sec * 1000 + tv.tv_usec / 1000;
  const int64_t t_ms = epoch_now_ms - (now_ms - m.t_ms);
  char hex[2 * PLM_EXT_FRAME_LEN + 1];
  frame_hex(m.msg, hex);
  char rssi[16];
  if (std::isnan(m.rssi)) {
    snprintf(rssi, sizeof(rssi), "null");
  } else {
    snprintf(rssi, sizeof(rssi), "%.1f", m.rssi);
  }

  if (this->decoded_logged_ < BRINGUP_DECODED) {
    this->decoded_logged_++;
    ESP_LOGI(TAG, "decoded %u/%u: %s, %u cop%s, hops left %u, %s dBm", (unsigned) this->decoded_logged_,
             (unsigned) BRINGUP_DECODED, hex, (unsigned) m.msg.copies, m.msg.copies == 1 ? "y" : "ies",
             (unsigned) m.msg.hops_left, rssi);
  }
#ifdef USE_MQTT
  if (this->decoded_topic_.empty() || mqtt::global_mqtt_client == nullptr)
    return;
  // Contract v1 (insteonrf/listener.py, ListenerMessage.to_dict). No repair
  // on the board, and a hardware demodulator has no SNR.
  char payload[256];
  snprintf(payload, sizeof(payload),
           "{\"v\":1,\"rx\":\"%s\",\"t\":%lld.%03d,\"frame\":\"%s\",\"copies\":%u,"
           "\"hops_left\":%u,\"rssi\":%s,\"snr\":null,\"repaired\":0}",
           App.get_name().c_str(), (long long) (t_ms / 1000), (int) (t_ms % 1000), hex,
           (unsigned) m.msg.copies, (unsigned) m.msg.hops_left, rssi);
  // QoS 0, not retained, like the captures: events, and the listeners are
  // redundant by construction.
  mqtt::global_mqtt_client->publish(this->decoded_topic_, std::string(payload), 0, false);
#endif
}

void InsteonRF::publish_capture_(const uint8_t *buf, size_t len, float rssi, uint32_t first_bit_us) {
#ifdef USE_MQTT
  if (this->mqtt_topic_.empty() || mqtt::global_mqtt_client == nullptr)
    return;

  static const char *const B64 =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string blob;
  blob.reserve((len + 2) / 3 * 4);
  for (size_t i = 0; i < len; i += 3) {
    const uint32_t a = buf[i];
    const uint32_t b = (i + 1 < len) ? buf[i + 1] : 0;
    const uint32_t c = (i + 2 < len) ? buf[i + 2] : 0;
    const uint32_t v = (a << 16) | (b << 8) | c;
    blob += B64[(v >> 18) & 0x3F];
    blob += B64[(v >> 12) & 0x3F];
    blob += (i + 1 < len) ? B64[(v >> 6) & 0x3F] : '=';
    blob += (i + 2 < len) ? B64[v & 0x3F] : '=';
  }

  // Epoch milliseconds of the capture's *first bit on the air*, so the host
  // can window captures from different receivers together; it falls back to
  // arrival time if this looks implausible, so an unsynced board is harmless.
  //
  // This used to be time(nullptr) * 1000 -- whole seconds, so up to a second
  // early at random -- taken at publish time. Against the other receivers
  // that alone split one transmission into several events. Now: the wall
  // clock at millisecond resolution, less how long ago the radio says the
  // first bit arrived.
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  const uint64_t now_ms = (uint64_t) tv.tv_sec * 1000ULL + (uint64_t) (tv.tv_usec / 1000);
  const uint64_t age_ms = (uint64_t) (micros() - first_bit_us) / 1000ULL;
  const uint64_t t_ms = now_ms - age_ms;
  // "sw" is the sync word this capture was matched on. The FIFO holds only
  // what follows it, so the host has to put the start header back before
  // parsing, and it needs to know which polarity to put back. "us" is the
  // board's own clock at that first bit: fine-grained within one board,
  // meaningless between boards.
  char head[192];
  snprintf(head, sizeof(head),
           "{\"n\":\"%s\",\"seq\":%u,\"t\":%llu,\"us\":%u,\"rssi\":%.1f,\"len\":%u,"
           "\"sw\":\"%08X\",\"b\":\"",
           App.get_name().c_str(), (unsigned) this->seq_, (unsigned long long) t_ms,
           (unsigned) first_bit_us, rssi, (unsigned) len, (unsigned) this->sync_word_);

  std::string payload(head);
  payload += blob;
  payload += "\"}";
  // QoS 0, not retained: these are events, and the mesh is redundant by
  // construction, so a redelivery is worth less than the broker overhead.
  mqtt::global_mqtt_client->publish(this->mqtt_topic_, payload, 0, false);
#else
  (void) buf;
  (void) len;
  (void) rssi;
  (void) first_bit_us;
#endif
}

void InsteonRF::dump_common_config_() {
  ESP_LOGCONFIG(TAG, "  Capture: %u bytes (%.0f ms of air)", (unsigned) this->capture_bytes_,
                this->capture_bytes_ * 8.0f * 1000.0f / INSTEON_BITRATE);
  ESP_LOGCONFIG(TAG, "  Manchester gate: %u frames, up to %u bad pairs",
                (unsigned) this->manchester_gate_, (unsigned) this->manchester_gate_errors_);
  ESP_LOGCONFIG(TAG, "  RSSI floor: %.1f dBm", this->rssi_floor_);
  static const char *const MODES[] = {"decoded", "captures", "both"};
  ESP_LOGCONFIG(TAG, "  Publish: %s", MODES[this->publish_mode_ % 3]);
  if (this->publish_decoded_enabled_()) {
    ESP_LOGCONFIG(TAG, "  Decoded topic: %s (fold window %u ms, released %u ms later)",
                  this->decoded_topic_.c_str(), (unsigned) FOLD_WINDOW_MS, (unsigned) RELEASE_LATENESS_MS);
  }
  if (this->publish_captures_()) {
    ESP_LOGCONFIG(TAG, "  Capture topic: %s", this->mqtt_topic_.c_str());
  }
  if (this->is_failed())
    ESP_LOGE(TAG, "  RADIO NOT RESPONDING");
}

}  // namespace insteon_rf
}  // namespace esphome
