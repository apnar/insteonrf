#include "sx1262.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cstring>

namespace esphome {
namespace insteon_rf {

static const char *const TAG = "insteon_rf.sx1262";

// --------------------------------------------------------------------- SPI plumbing

void InsteonRFSX1262::wait_busy_(uint32_t timeout_ms) {
  if (this->busy_pin_ == nullptr)
    return;
  const uint32_t start = millis();
  while (this->busy_pin_->digital_read()) {
    if (millis() - start > timeout_ms) {
      ESP_LOGW(TAG, "BUSY stuck high for %ums", (unsigned) timeout_ms);
      return;
    }
    delayMicroseconds(50);
  }
}

void InsteonRFSX1262::cmd_(uint8_t opcode, const uint8_t *data, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(opcode);
  for (size_t i = 0; i < len; i++)
    this->write_byte(data[i]);
  this->disable();
}

void InsteonRFSX1262::read_cmd_(uint8_t opcode, uint8_t *out, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(opcode);
  this->write_byte(0x00);  // status byte
  for (size_t i = 0; i < len; i++)
    out[i] = this->transfer_byte(0x00);
  this->disable();
}

void InsteonRFSX1262::write_register_(uint16_t addr, const uint8_t *data, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(SX_WRITE_REGISTER);
  this->write_byte(addr >> 8);
  this->write_byte(addr & 0xFF);
  for (size_t i = 0; i < len; i++)
    this->write_byte(data[i]);
  this->disable();
}

void InsteonRFSX1262::read_register_(uint16_t addr, uint8_t *out, size_t len) {
  this->wait_busy_();
  this->enable();
  this->write_byte(SX_READ_REGISTER);
  this->write_byte(addr >> 8);
  this->write_byte(addr & 0xFF);
  this->write_byte(0x00);  // status byte
  for (size_t i = 0; i < len; i++)
    out[i] = this->transfer_byte(0x00);
  this->disable();
}

void InsteonRFSX1262::read_buffer_(uint8_t offset, uint8_t *out, size_t len) {
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

void InsteonRFSX1262::setup() {
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
    ESP_LOGE(TAG, "SX1262 not responding: check CS/MISO/MOSI/BUSY wiring and the TCXO setting");
    this->mark_failed();
    return;
  }
  this->radio_ok_ = true;
  this->start_rx_();
  this->high_freq_.start();
  this->last_minute_mark_ = millis();
}

bool InsteonRFSX1262::configure_radio_() {
  uint8_t buf[9];

  buf[0] = 0x00;  // STDBY_RC
  this->cmd_(SX_SET_STANDBY, buf, 1);

  // A radio that never syncs and never errors is almost always a wrong TCXO
  // setting: without it the crystal never starts and calibration silently
  // produces a deaf receiver. The Heltec V3 drives its TCXO from DIO3.
  // The start-up delay is in units of 15.625 us.
  buf[0] = this->tcxo_voltage_;
  const uint32_t delay_ticks =
      (uint32_t) (((uint64_t) this->tcxo_delay_us_ * 1000ULL) / 15625ULL);
  buf[1] = (delay_ticks >> 16) & 0xFF;
  buf[2] = (delay_ticks >> 8) & 0xFF;
  buf[3] = delay_ticks & 0xFF;
  this->cmd_(SX_SET_DIO3_TCXO, buf, 4);

  buf[0] = 0x7F;  // recalibrate everything after enabling the TCXO
  this->cmd_(SX_CALIBRATE, buf, 1);
  delay(5);
  this->wait_busy_();

  buf[0] = 0x01;  // DC-DC; the Heltec boards are wired for it
  this->cmd_(SX_SET_REGULATOR_MODE, buf, 1);
  buf[0] = 0x01;  // DIO2 drives the RF switch
  this->cmd_(SX_SET_DIO2_RF_SWITCH, buf, 1);

  // SetPacketType must precede every other modem setting; it resets them.
  buf[0] = 0x00;  // GFSK
  this->cmd_(SX_SET_PACKET_TYPE, buf, 1);

  // Sync word, most significant byte first in the register block. Written
  // and then read back: this is the presence check. A status byte is
  // useless for that -- with no chip on the bus MISO floats high and the
  // status reads as 0xFF, which looks like a chip. A register that reads
  // back what was written cannot be faked by a floating line, and it also
  // catches CS on the wrong pin, which is the likeliest wiring mistake.
  // The low sync_bits_ of the word, most significant byte first: the chip
  // matches the first sync_bits_ bits of the register block.
  uint8_t sync[8] = {0};
  for (uint8_t i = 0; i < this->sync_bits_ / 8; i++)
    sync[i] = (this->sync_word_ >> (this->sync_bits_ - 8 * (i + 1))) & 0xFF;
  this->write_register_(SX_REG_SYNC_WORD_0, sync, 8);
  uint8_t check[8] = {0};
  this->read_register_(SX_REG_SYNC_WORD_0, check, 8);
  if (memcmp(sync, check, sizeof(sync)) != 0) {
    ESP_LOGE(TAG, "sync word readback mismatch: wrote %02X%02X%02X%02X, read %02X%02X%02X%02X",
             sync[0], sync[1], sync[2], sync[3], check[0], check[1], check[2], check[3]);
    return false;
  }

  // 902-928 MHz image calibration.
  buf[0] = 0xE1;
  buf[1] = 0xE9;
  this->cmd_(SX_CALIBRATE_IMAGE, buf, 2);

  const uint64_t frf = ((uint64_t) this->frequency_hz_ << 25) / SX126X_XTAL_HZ;
  buf[0] = (frf >> 24) & 0xFF;
  buf[1] = (frf >> 16) & 0xFF;
  buf[2] = (frf >> 8) & 0xFF;
  buf[3] = frf & 0xFF;
  this->cmd_(SX_SET_RF_FREQUENCY, buf, 4);

  // br = 32 * F_XTAL / bitrate, fdev = deviation * 2^25 / F_XTAL.
  const uint32_t br = (uint32_t) ((32ULL * SX126X_XTAL_HZ) / INSTEON_BITRATE);
  const uint32_t fdev = (uint32_t) (((uint64_t) INSTEON_DEVIATION_HZ << 25) / SX126X_XTAL_HZ);
  buf[0] = (br >> 16) & 0xFF;
  buf[1] = (br >> 8) & 0xFF;
  buf[2] = br & 0xFF;
  buf[3] = 0x00;  // no pulse shaping: Insteon is plain 2-FSK
  buf[4] = SX_GFSK_BW_234_3;
  buf[5] = (fdev >> 16) & 0xFF;
  buf[6] = (fdev >> 8) & 0xFF;
  buf[7] = fdev & 0xFF;
  this->cmd_(SX_SET_MODULATION_PARAMS, buf, 8);

  // GFSK packet parameters. NINE bytes -- the ninth is whitening, and an
  // earlier revision sent eight, leaving whitening undefined. Enabled
  // whitening XORs every captured byte with a PN9 sequence, which fails the
  // Manchester gate on every capture and makes a perfectly wired board look
  // dead. (Register 0x06B8, which that revision then poked, is the whitening
  // *seed*, not an enable.)
  buf[0] = 0x00;  // TX preamble length, unused here
  buf[1] = 0x10;
  // Preamble detector default OFF. Insteon's preamble is a repeating 0110
  // cell, not the 0x55/0xAA alternation the detector expects, so leaving it
  // on may mean never syncing. The Manchester gate absorbs the extra false
  // syncs. Exposed in YAML so it can be tried without recompiling.
  buf[2] = this->preamble_detector_;
  buf[3] = this->sync_bits_;   // sync word length, in bits
  buf[4] = 0x00;               // no address filtering: Insteon addresses are elsewhere
  buf[5] = 0x00;               // fixed length: Insteon has no length field the chip can read
  buf[6] = this->capture_bytes_;
  buf[7] = 0x01;  // CRC off: Insteon's CRC is its own algorithm
  buf[8] = 0x00;  // whitening off: it would destroy the payload
  this->cmd_(SX_SET_PACKET_PARAMS, buf, 9);

  // Boosted RX gain: about 2 dB more sensitivity for about 2 mA, which a
  // mains-powered listener should always take.
  buf[0] = 0x96;
  this->write_register_(SX_REG_RX_GAIN, buf, 1);

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

  return true;
}

void InsteonRFSX1262::start_rx_() {
  uint8_t clear[2] = {SX_IRQ_ALL >> 8, SX_IRQ_ALL & 0xFF};
  this->cmd_(SX_CLEAR_IRQ_STATUS, clear, 2);
  uint8_t rx[3] = {0xFF, 0xFF, 0xFF};  // continuous
  this->cmd_(SX_SET_RX, rx, 3);
}

float InsteonRFSX1262::read_rssi_() {
  // GetPacketStatus for GFSK returns RxStatus, RssiSync, RssiAvg; RssiAvg is
  // averaged over the packet just received. GetRssiInst is instantaneous,
  // and read after a capture it mostly measured whatever was on air *next* --
  // a few feet from the PLM that was its hop repeat, which is how a distant
  // device's ACK came to report -46 dBm on the first bring-up. RSSI is the
  // whole point of the placement survey, so it has to be the packet's own.
  uint8_t ps[3] = {0, 0, 0};
  this->read_cmd_(SX_GET_PACKET_STATUS, ps, 3);
  return -((float) ps[2]) / 2.0f;
}

// --------------------------------------------------------------------- loop

void InsteonRFSX1262::loop() {
  if (!this->radio_ok_)
    return;

  this->tick_minute_();

  // DIO1 is mapped to RxDone|Timeout, so its level answers "anything to do?"
  // for free. Without this, polling the IRQ register is a 4-byte SPI
  // transaction on every loop iteration -- roughly 1 kHz -- competing with
  // WiFi and MQTT for the core. Polling (rather than an ISR) is still right:
  // a capture is 112 ms of air and the FIFO holds it, so there is nothing to
  // race, and it keeps SPI on the main task where ESPHome expects it.
  if (this->dio1_pin_ != nullptr && !this->dio1_pin_->digital_read())
    return;

  uint8_t irq[2] = {0, 0};
  this->read_cmd_(SX_GET_IRQ_STATUS, irq, 2);
  const uint16_t status = ((uint16_t) irq[0] << 8) | irq[1];
  if (status == 0)
    return;

  this->rx_done_us_ = micros();
  uint8_t clear[2] = {irq[0], irq[1]};
  this->cmd_(SX_CLEAR_IRQ_STATUS, clear, 2);

  if (status & SX_IRQ_RX_DONE)
    this->handle_capture_();
  // Continuous RX re-arms itself after every packet: the chip is already
  // hunting for the next sync word by the time we get here. Issuing SetRx
  // again restarted it, and anything that had begun to arrive in the
  // meantime -- a hop repeat or an ACK 50 ms behind the capture just read,
  // with WiFi having held this loop up -- was thrown away. Only a timeout
  // (which continuous mode should never raise) needs restarting.
  if (!(status & SX_IRQ_RX_DONE))
    this->start_rx_();
}

void InsteonRFSX1262::handle_capture_() {
  const float rssi = this->read_rssi_();

  // In continuous RX the chip advances its buffer pointer between packets,
  // so ask where this one starts rather than assuming the base address.
  uint8_t bs[2] = {0, 0};
  this->read_cmd_(SX_GET_RX_BUFFER_STATUS, bs, 2);  // PayloadLengthRx, RxStartBufferPointer
  const uint8_t got = bs[0];
  const uint8_t start = bs[1];
  const size_t len = std::min<size_t>(got ? got : this->capture_bytes_, sizeof(this->buffer_));
  this->read_buffer_(start, this->buffer_, len);

  // RxDone fires when the last byte is in, so the first bit after the sync
  // word was on the air one capture-length earlier.
  this->process_capture_(this->buffer_, len, rssi, this->rx_done_us_ - air_us(len));
}

void InsteonRFSX1262::dump_config() {
  ESP_LOGCONFIG(TAG, "Insteon RF listener, SX1262 (receive only):");
  ESP_LOGCONFIG(TAG, "  Frequency: %.3f MHz", this->frequency_hz_ / 1e6f);
  ESP_LOGCONFIG(TAG, "  Bitrate: %u baud, deviation %u Hz, RX bandwidth 234.3 kHz",
                (unsigned) INSTEON_BITRATE, (unsigned) INSTEON_DEVIATION_HZ);
  ESP_LOGCONFIG(TAG, "  Sync word: 0x%08X (low %u bits)%s", (unsigned) this->sync_word_,
                (unsigned) this->sync_bits_,
                this->sync_word_ == INSTEON_SYNC_WORD ? "" : "  [non-default]");
  ESP_LOGCONFIG(TAG, "  Preamble detector: %s", this->preamble_detector_ ? "on" : "off");
  LOG_PIN("  CS Pin: ", this->cs_);
  LOG_PIN("  RESET Pin: ", this->reset_pin_);
  LOG_PIN("  BUSY Pin: ", this->busy_pin_);
  LOG_PIN("  DIO1 Pin: ", this->dio1_pin_);
  this->dump_common_config_();
}

}  // namespace insteon_rf
}  // namespace esphome
