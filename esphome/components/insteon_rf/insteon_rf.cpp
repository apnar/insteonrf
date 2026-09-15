#include "insteon_rf.h"
#include "esphome/core/log.h"

#ifdef USE_MQTT
#include "esphome/components/mqtt/mqtt_client.h"
#endif

#include <cstdio>

namespace esphome {
namespace insteon_rf {

static const char *const TAG = "insteon_rf";

// --------------------------------------------------------------------- SPI plumbing

void InsteonRF::wait_busy_(uint32_t timeout_ms) {
  if (this->busy_pin_ == nullptr)
    return;
  const uint32_t start = millis();
  while (this->busy_pin_->digital_read()) {
    if (millis() - start > timeout_ms) {
      ESP_LOGW(TAG, "BUSY stuck high for %ums", timeout_ms);
      return;
    }
    delayMicroseconds(50);
  }
}

void InsteonRF::cmd_(uint8_t opcode, const uint8_t *data, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(opcode);
  for (size_t i = 0; i < len; i++)
    this->write_byte(data[i]);
  this->disable();
}

void InsteonRF::read_cmd_(uint8_t opcode, uint8_t *out, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(opcode);
  this->write_byte(0x00);  // status byte
  for (size_t i = 0; i < len; i++)
    out[i] = this->transfer_byte(0x00);
  this->disable();
}

void InsteonRF::write_register_(uint16_t addr, const uint8_t *data, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(SX_WRITE_REGISTER);
  this->write_byte(addr >> 8);
  this->write_byte(addr & 0xFF);
  for (size_t i = 0; i < len; i++)
    this->write_byte(data[i]);
  this->disable();
}

void InsteonRF::read_buffer_(uint8_t offset, uint8_t *out, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(SX_READ_BUFFER);
  this->write_byte(offset);
  this->write_byte(0x00);  // status byte
  for (size_t i = 0; i < len; i++)
    out[i] = this->transfer_byte(0x00);
  this->disable();
}

// --------------------------------------------------------------------- setup

void InsteonRF::setup() {
  ESP_LOGCONFIG(TAG, "Setting up Insteon RF listener...");
  this->spi_setup();

  if (this->busy_pin_ != nullptr)
    this->busy_pin_->setup();
  if (this->dio1_pin_ != nullptr)
    this->dio1_pin_->setup();

  if (this->reset_pin_ != nullptr) {
    this->reset_pin_->setup();
    this->reset_pin_->digital_write(false);
    delay(10);
    this->reset_pin_->digital_write(true);
    delay(20);
  }

  if (!this->configure_radio_()) {
    ESP_LOGE(TAG, "SX1262 did not respond; check CS/BUSY wiring and the TCXO setting");
    this->mark_failed();
    return;
  }
  this->radio_ok_ = true;
  this->start_rx_();
  this->last_minute_mark_ = millis();
}

bool InsteonRF::configure_radio_() {
  uint8_t buf[8];

  buf[0] = 0x00;  // STDBY_RC
  this->cmd_(SX_SET_STANDBY, buf, 1);

  // A radio that never syncs and never errors is almost always a wrong TCXO
  // setting: without it the crystal never starts and calibration silently
  // produces a deaf receiver. The Heltec V3 drives its TCXO from DIO3.
  buf[0] = this->tcxo_voltage_;
  const uint32_t delay_ticks = this->tcxo_delay_us_ / 15625 * 1000 + this->tcxo_delay_us_ / 15;
  buf[1] = (delay_ticks >> 16) & 0xFF;
  buf[2] = (delay_ticks >> 8) & 0xFF;
  buf[3] = delay_ticks & 0xFF;
  this->cmd_(SX_SET_DIO3_TCXO, buf, 4);

  buf[0] = 0x7F;  // recalibrate everything after enabling the TCXO
  this->cmd_(SX_CALIBRATE, buf, 1);
  delay(5);
  this->wait_busy_();

  // Probe before configuring: if the chip is not there, everything below is
  // writes into the void and the failure surfaces much later as silence.
  uint8_t status = 0;
  this->read_cmd_(SX_GET_STATUS, &status, 1);

  buf[0] = 0x01;  // DC-DC; the Heltec boards are wired for it
  this->cmd_(SX_SET_REGULATOR_MODE, buf, 1);
  buf[0] = 0x01;  // DIO2 drives the RF switch
  this->cmd_(SX_SET_DIO2_RF_SWITCH, buf, 1);

  buf[0] = 0x00;  // GFSK
  this->cmd_(SX_SET_PACKET_TYPE, buf, 1);

  // 915 MHz band image calibration.
  buf[0] = 0xE1;
  buf[1] = 0xE9;
  this->cmd_(SX_CALIBRATE_IMAGE, buf, 2);

  const uint64_t frf =
      ((uint64_t) this->frequency_hz_ << 25) / SX126X_XTAL_HZ;
  buf[0] = (frf >> 24) & 0xFF;
  buf[1] = (frf >> 16) & 0xFF;
  buf[2] = (frf >> 8) & 0xFF;
  buf[3] = frf & 0xFF;
  this->cmd_(SX_SET_RF_FREQUENCY, buf, 4);

  // br = 32 * F_XTAL / bitrate, fdev = deviation * 2^25 / F_XTAL.
  const uint32_t br = (uint32_t) ((32ULL * SX126X_XTAL_HZ) / INSTEON_BITRATE);
  const uint32_t fdev =
      (uint32_t) (((uint64_t) INSTEON_DEVIATION_HZ << 25) / SX126X_XTAL_HZ);
  buf[0] = (br >> 16) & 0xFF;
  buf[1] = (br >> 8) & 0xFF;
  buf[2] = br & 0xFF;
  buf[3] = 0x00;  // no pulse shaping: Insteon is plain 2-FSK
  buf[4] = SX_GFSK_BW_234_3;
  buf[5] = (fdev >> 16) & 0xFF;
  buf[6] = (fdev >> 8) & 0xFF;
  buf[7] = fdev & 0xFF;
  this->cmd_(SX_SET_MODULATION_PARAMS, buf, 8);

  // Sync word, most significant byte first in the register block.
  uint8_t sync[8] = {0};
  for (uint8_t i = 0; i < INSTEON_SYNC_BITS / 8; i++)
    sync[i] = (INSTEON_SYNC_WORD >> (INSTEON_SYNC_BITS - 8 * (i + 1))) & 0xFF;
  this->write_register_(SX_REG_SYNC_WORD_0, sync, 8);

  buf[0] = 0x00;  // TX preamble length, unused here
  buf[1] = 0x10;
  // Preamble detector OFF. Insteon's preamble is a repeating 0110 cell, not
  // the 0x55/0xAA alternation the detector expects, so leaving it on means
  // never syncing. The Manchester gate below absorbs the extra false syncs.
  buf[2] = 0x00;
  buf[3] = INSTEON_SYNC_BITS;
  buf[4] = 0x00;  // no address filtering: Insteon addresses are elsewhere
  buf[5] = 0x00;  // fixed length: Insteon has no length field the chip can read
  buf[6] = this->capture_bytes_;
  buf[7] = 0x01;  // CRC off: Insteon's CRC is its own algorithm
  this->cmd_(SX_SET_PACKET_PARAMS, buf, 8);
  buf[0] = 0x00;  // whitening off; it would destroy the payload
  this->write_register_(0x06B8, buf, 1);

  buf[0] = 0x00;
  buf[1] = 0x00;
  this->cmd_(SX_SET_BUFFER_BASE, buf, 2);

  buf[0] = (SX_IRQ_RX_DONE | SX_IRQ_TIMEOUT) >> 8;
  buf[1] = (SX_IRQ_RX_DONE | SX_IRQ_TIMEOUT) & 0xFF;
  buf[2] = buf[0];  // DIO1 mask
  buf[3] = buf[1];
  buf[4] = 0x00;
  buf[5] = 0x00;
  buf[6] = 0x00;
  buf[7] = 0x00;
  this->cmd_(SX_SET_DIO_IRQ_PARAMS, buf, 8);

  return status != 0x00;
}

void InsteonRF::start_rx_() {
  uint8_t clear[2] = {SX_IRQ_ALL >> 8, SX_IRQ_ALL & 0xFF};
  this->cmd_(SX_CLEAR_IRQ_STATUS, clear, 2);
  uint8_t rx[3] = {0xFF, 0xFF, 0xFF};  // continuous
  this->cmd_(SX_SET_RX, rx, 3);
}

float InsteonRF::read_rssi_() {
  uint8_t v = 0;
  this->read_cmd_(SX_GET_RSSI_INST, &v, 1);
  return -((float) v) / 2.0f;
}

// --------------------------------------------------------------------- the gate

bool InsteonRF::manchester_gate_ok_(const uint8_t *buf, size_t len) const {
  const size_t nbits = len * 8;
  auto bit = [buf](size_t i) -> uint8_t {
    return (buf[i >> 3] >> (7 - (i & 7))) & 1;
  };
  // Roughly 26 of every 28 on-air bits are Manchester pairs, and a valid pair
  // is only 01 or 10 -- never 00 or 11. Random noise fails within a handful
  // of bits, which is what makes this cheap check enough to run with the
  // preamble detector off in a crowded 915 MHz band.
  //
  // Layout after the sync word. The sync word swallowed the preamble, the
  // literal '11' marker and the first 9 bits of the Manchester-coded frame
  // index 31, so the FIFO starts one bit into that index:
  //
  //   bit 0        the last bit of Manchester(index 31)
  //   bits 1..16   Manchester(data byte 0), 8 pairs
  //   bit 17..     frame 2: '11' marker (inverted to 00), then 5 index pairs
  //                and 8 data pairs; 28 bits per frame thereafter.
  if (nbits < 17)
    return false;
  if (bit(0) != 0)
    return false;
  for (uint8_t j = 0; j < 8; j++) {
    const size_t at = 1 + 2 * j;
    if (bit(at) == bit(at + 1))
      return false;
  }
  for (uint8_t k = 0; k + 1 < this->manchester_gate_; k++) {
    const size_t base = 17 + (size_t) k * 28;
    if (base + 28 > nbits)
      break;
    if (bit(base) != 0 || bit(base + 1) != 0)
      return false;  // the inverted '11' frame marker
    for (uint8_t j = 0; j < 13; j++) {
      const size_t at = base + 2 + 2 * j;
      if (bit(at) == bit(at + 1))
        return false;
    }
  }
  return true;
}

// --------------------------------------------------------------------- loop

void InsteonRF::loop() {
  if (!this->radio_ok_)
    return;

  const uint32_t now = millis();
  if (now - this->last_minute_mark_ >= 60000) {
#ifdef USE_SENSOR
    if (this->captures_sensor_ != nullptr)
      this->captures_sensor_->publish_state(this->captures_ - this->captures_at_mark_);
    if (this->accepted_sensor_ != nullptr)
      this->accepted_sensor_->publish_state(this->accepted_ - this->accepted_at_mark_);
#endif
    this->captures_at_mark_ = this->captures_;
    this->accepted_at_mark_ = this->accepted_;
    this->last_minute_mark_ = now;
  }

  // Poll the IRQ rather than using an interrupt: a capture is 112 ms of air
  // and the FIFO holds it, so there is nothing to race, and polling keeps the
  // SPI traffic on the main task where ESPHome expects it.
  uint8_t irq[2] = {0, 0};
  this->read_cmd_(SX_GET_IRQ_STATUS, irq, 2);
  const uint16_t status = ((uint16_t) irq[0] << 8) | irq[1];
  if (status == 0)
    return;

  uint8_t clear[2] = {irq[0], irq[1]};
  this->cmd_(SX_CLEAR_IRQ_STATUS, clear, 2);

  if (status & SX_IRQ_RX_DONE)
    this->handle_capture_();
  this->start_rx_();
}

void InsteonRF::handle_capture_() {
  this->captures_++;
  const float rssi = this->read_rssi_();
  this->read_buffer_(0, this->buffer_, this->capture_bytes_);

#ifdef USE_SENSOR
  if (this->last_rssi_sensor_ != nullptr)
    this->last_rssi_sensor_->publish_state(rssi);
#endif

  if (rssi < this->rssi_floor_) {
    ESP_LOGV(TAG, "dropping capture at %.1f dBm (floor %.1f)", rssi, this->rssi_floor_);
    return;
  }
  if (!this->manchester_gate_ok_(this->buffer_, this->capture_bytes_)) {
    ESP_LOGV(TAG, "capture failed the Manchester gate");
    return;
  }

  this->accepted_++;
  this->seq_++;
  this->publish_capture_(this->buffer_, this->capture_bytes_, rssi);
}

void InsteonRF::publish_capture_(const uint8_t *buf, size_t len, float rssi) {
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

  // Epoch milliseconds when the clock is set, so the host can window
  // captures from different boards together; it falls back to arrival time
  // if this looks implausible, so an unsynced board is harmless.
  char head[160];
  const uint32_t us = micros();
  snprintf(head, sizeof(head),
           "{\"n\":\"%s\",\"seq\":%u,\"t\":%llu,\"us\":%u,\"rssi\":%.1f,\"len\":%u,\"b\":\"",
           App.get_name().c_str(), (unsigned) this->seq_,
           (unsigned long long) ((uint64_t) time(nullptr) * 1000ULL), (unsigned) us, rssi,
           (unsigned) len);

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
#endif
}

void InsteonRF::dump_config() {
  ESP_LOGCONFIG(TAG, "Insteon RF listener (receive only):");
  ESP_LOGCONFIG(TAG, "  Frequency: %.3f MHz", this->frequency_hz_ / 1e6f);
  ESP_LOGCONFIG(TAG, "  Bitrate: %u baud, deviation %u Hz, RX bandwidth 234.3 kHz",
                (unsigned) INSTEON_BITRATE, (unsigned) INSTEON_DEVIATION_HZ);
  ESP_LOGCONFIG(TAG, "  Sync word: 0x%08X (%u bits)", (unsigned) INSTEON_SYNC_WORD,
                (unsigned) INSTEON_SYNC_BITS);
  ESP_LOGCONFIG(TAG, "  Capture: %u bytes (%.0f ms of air)", (unsigned) this->capture_bytes_,
                this->capture_bytes_ * 8.0f * 1000.0f / INSTEON_BITRATE);
  ESP_LOGCONFIG(TAG, "  Manchester gate: %u frames", (unsigned) this->manchester_gate_);
  ESP_LOGCONFIG(TAG, "  RSSI floor: %.1f dBm", this->rssi_floor_);
  ESP_LOGCONFIG(TAG, "  MQTT topic: %s", this->mqtt_topic_.c_str());
  LOG_PIN("  CS Pin: ", this->cs_);
  LOG_PIN("  RESET Pin: ", this->reset_pin_);
  LOG_PIN("  BUSY Pin: ", this->busy_pin_);
  LOG_PIN("  DIO1 Pin: ", this->dio1_pin_);
  if (this->is_failed())
    ESP_LOGE(TAG, "  RADIO NOT RESPONDING");
}

}  // namespace insteon_rf
}  // namespace esphome
