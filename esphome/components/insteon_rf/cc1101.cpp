#include "cc1101.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cmath>

namespace esphome {
namespace insteon_rf {

static const char *const TAG = "insteon_rf.cc1101";

// Hand-off queue depth. A capture is 142 ms of air, so eight is over a
// second of back-to-back traffic waiting on loop(); if that fills, loop() is
// wedged and dropping captures is the right answer.
static const UBaseType_t QUEUE_DEPTH = 8;
// Above loopTask (1) on the same core, below WiFi (23) and lwIP (18).
static const UBaseType_t TASK_PRIORITY = 5;
static const BaseType_t TASK_CORE = 1;
static const uint32_t TASK_STACK = 4096;
// Without GDO pins, poll the FIFO this often; with them, still do an SPI
// poll at this interval in case an edge was missed. Either is far inside
// the 56 ms the FIFO lasts.
static const uint32_t SPI_POLL_MS = 2;
static const uint32_t SPI_FALLBACK_POLL_MS = 20;
// A packet streams a byte every 877 us. A capture that makes no progress
// for this long is not going to finish.
static const uint32_t STALL_US = 20000;
// RX never leaves RX on its own (RXOFF_MODE = stay), so the synthesiser is
// calibrated only when it is restarted. Recalibrate between packets this
// often to track temperature.
static const uint32_t RECAL_MS = 15 * 60 * 1000;
static const uint32_t HEALTH_MS = 1000;

// --------------------------------------------------------------------- SPI plumbing

uint8_t InsteonRFCC1101::strobe_(uint8_t cmd) {
  this->enable();
  const uint8_t status = this->transfer_byte(cmd);
  this->disable();
  return status;
}

void InsteonRFCC1101::write_reg_(uint8_t addr, uint8_t value) {
  this->enable();
  this->write_byte(addr);
  this->write_byte(value);
  this->disable();
}

uint8_t InsteonRFCC1101::read_reg_(uint8_t addr) {
  this->enable();
  this->write_byte(addr | CC_READ);
  const uint8_t v = this->transfer_byte(0x00);
  this->disable();
  return v;
}

uint8_t InsteonRFCC1101::read_status_(uint8_t addr) {
  // Status registers share addresses with the strobes; the burst bit is
  // what tells the chip this is a read rather than a command.
  this->enable();
  this->write_byte(addr | CC_READ | CC_BURST);
  const uint8_t v = this->transfer_byte(0x00);
  this->disable();
  return v;
}

uint8_t InsteonRFCC1101::read_rxbytes_() {
  // Errata (SWRZ020): RXBYTES can be read while it is being updated, giving
  // a wrong count. Read until two consecutive reads agree.
  uint8_t a = this->read_status_(CC_RXBYTES);
  for (uint8_t i = 0; i < 4; i++) {
    const uint8_t b = this->read_status_(CC_RXBYTES);
    if (a == b)
      return a;
    a = b;
  }
  return a;
}

void InsteonRFCC1101::read_fifo_(uint8_t *out, size_t n) {
  this->enable();
  this->write_byte(CC_FIFO | CC_READ | CC_BURST);
  this->read_array(out, n);
  this->disable();
}

// --------------------------------------------------------------------- setup

void InsteonRFCC1101::setup() {
  ESP_LOGCONFIG(TAG, "Setting up Insteon RF listener (CC1101)...");
  this->spi_setup();

  if (this->gdo0_pin_ != nullptr)
    this->gdo0_pin_->setup();
  if (this->gdo2_pin_ != nullptr)
    this->gdo2_pin_->setup();

  // The board's RF switch picks one of three band filters. Left on the
  // 315 or 433 MHz path the radio still syncs on nothing and reports
  // nothing, which looks like a quiet house -- so it is set here, from the
  // frequency, rather than left to a YAML switch someone might forget.
  // LilyGO: SW1=1 SW0=0 315 MHz, SW1=1 SW0=1 433 MHz, SW1=0 SW0=1 868/915.
  const bool sub_ghz_high = this->frequency_hz_ >= 779000000UL;
  if (this->sw1_pin_ != nullptr) {
    this->sw1_pin_->setup();
    this->sw1_pin_->digital_write(!sub_ghz_high);
  }
  if (this->sw0_pin_ != nullptr) {
    this->sw0_pin_->setup();
    this->sw0_pin_->digital_write(this->frequency_hz_ >= 400000000UL);
  }

  // The chip is on a switched supply (BOARD_PWR_EN), turned on at HARDWARE
  // priority; give the crystal time to start before the reset.
  delay(5);
  this->strobe_(CC_SRES);
  delay(5);

  if (!this->configure_radio_()) {
    this->mark_failed();
    return;
  }

  this->queue_ = xQueueCreate(QUEUE_DEPTH, sizeof(CC1101Capture));
  if (this->queue_ == nullptr) {
    ESP_LOGE(TAG, "could not allocate the capture queue");
    this->mark_failed();
    return;
  }

  this->restart_rx_();
  this->radio_ok_ = true;
  this->last_minute_mark_ = millis();
  this->last_recal_ms_ = millis();

  // From here on the radio task owns the CC1101: nothing else touches its
  // registers, so the SPI conversation never has to be shared between tasks
  // for one device. The bus itself is shared with the display, and ESPHome's
  // SPI delegate takes the ESP-IDF bus lock around every transaction, so
  // the two cannot interleave.
  if (xTaskCreatePinnedToCore(&InsteonRFCC1101::task_entry_, "insteon_rf", TASK_STACK, this, TASK_PRIORITY,
                              &this->task_, TASK_CORE) != pdPASS) {
    ESP_LOGE(TAG, "could not start the radio task");
    this->radio_ok_ = false;
    this->mark_failed();
  }
}

bool InsteonRFCC1101::configure_radio_() {
  // Presence first. A status byte proves nothing -- with no chip on the bus
  // MISO floats high and everything reads 0xFF -- so check the part number
  // and version, then write a register and read it back. That also catches
  // CS on the wrong pin, the likeliest wiring mistake, and a powered-down
  // chip (PWR_EN low), the likeliest configuration mistake.
  const uint8_t partnum = this->read_status_(CC_PARTNUM);
  this->chip_version_ = this->read_status_(CC_VERSION);
  if (partnum != 0x00 || this->chip_version_ == 0x00 || this->chip_version_ == 0xFF) {
    ESP_LOGE(TAG, "CC1101 not responding (PARTNUM 0x%02X, VERSION 0x%02X): check CS/MISO wiring and "
                  "that BOARD_PWR_EN (GPIO15 on the T-Embed) is driven high",
             partnum, this->chip_version_);
    return false;
  }
  const uint8_t sync1 = (this->sync_word_ >> 8) & 0xFF;
  const uint8_t sync0 = this->sync_word_ & 0xFF;
  this->write_reg_(CC_SYNC1, sync1);
  this->write_reg_(CC_SYNC0, sync0);
  const uint8_t back1 = this->read_reg_(CC_SYNC1);
  const uint8_t back0 = this->read_reg_(CC_SYNC0);
  if (back1 != sync1 || back0 != sync0) {
    ESP_LOGE(TAG, "sync word readback mismatch: wrote %02X%02X, read %02X%02X", sync1, sync0, back1, back0);
    return false;
  }

  this->strobe_(CC_SIDLE);

  // GDO2: asserts when the RX FIFO holds at least the threshold (8 bytes,
  // below), so "anything to read?" is a GPIO read instead of an SPI one.
  // GDO0: asserts at sync, deasserts at the end of the capture.
  // GDO1 is the SPI SO line; leave it alone.
  this->write_reg_(CC_IOCFG2, 0x00);
  this->write_reg_(CC_IOCFG1, 0x2E);
  this->write_reg_(CC_IOCFG0, 0x06);
  // ADC_RETENTION, CLOSE_IN_RX (front-end attenuation for a board parked
  // beside a transmitter -- the V4's lesson), RX threshold 8 bytes: the task
  // wakes with 56 bytes of headroom, 49 ms.
  this->write_reg_(CC_FIFOTHR, 0x40 | ((this->rx_attenuation_ & 0x03) << 4) | 0x01);

  // A raw bit recorder, like the dongle: fixed length, no preamble quality
  // gate, no address check, no status bytes, no CRC, no whitening. The
  // chip matches the start header and copies whatever follows.
  this->write_reg_(CC_PKTLEN, this->capture_bytes_);
  this->write_reg_(CC_PKTCTRL1, 0x00);
  this->write_reg_(CC_PKTCTRL0, 0x00);
  this->write_reg_(CC_ADDR, 0x00);
  this->write_reg_(CC_CHANNR, 0x00);

  // IF 152 kHz (SmartRF's value for this bandwidth), no frequency trim.
  this->write_reg_(CC_FSCTRL1, 0x06);
  this->write_reg_(CC_FSCTRL0, 0x00);

  const uint32_t freq = (uint32_t) llround((double) this->frequency_hz_ * 65536.0 / CC1101_XTAL_HZ);
  this->write_reg_(CC_FREQ2, (freq >> 16) & 0xFF);
  this->write_reg_(CC_FREQ1, (freq >> 8) & 0xFF);
  this->write_reg_(CC_FREQ0, freq & 0xFF);

  // Channel filter: the smallest step at or above the request.
  // BW = f_xtal / (8 * (4 + M) * 2^E).
  uint8_t bw_e = 0, bw_m = 0;
  double best_bw = 0;
  for (int e = 3; e >= 0; e--) {
    for (int m = 3; m >= 0; m--) {
      const double bw = (double) CC1101_XTAL_HZ / (8.0 * (4 + m) * (1 << e));
      if (bw >= this->bandwidth_hz_ && (best_bw == 0 || bw < best_bw)) {
        best_bw = bw;
        bw_e = e;
        bw_m = m;
      }
    }
  }
  if (best_bw == 0) {  // wider than any setting: take the widest
    best_bw = (double) CC1101_XTAL_HZ / 32.0;
  }
  this->actual_bandwidth_hz_ = best_bw;

  // Data rate: R = (256 + M) * 2^E * f_xtal / 2^28.
  uint8_t dr_e = 0, dr_m = 0;
  for (int e = 0; e < 16; e++) {
    const double m = (double) INSTEON_BITRATE * 268435456.0 / ((double) CC1101_XTAL_HZ * (1 << e)) - 256.0;
    if (m >= -0.5 && m < 255.5) {
      dr_e = e;
      dr_m = (uint8_t) lround(m);
      break;
    }
  }
  this->actual_bitrate_ = (256.0f + dr_m) * (float) (1 << dr_e) * (float) CC1101_XTAL_HZ / 268435456.0f;
  this->write_reg_(CC_MDMCFG4, (bw_e << 6) | (bw_m << 4) | dr_e);
  this->write_reg_(CC_MDMCFG3, dr_m);

  // DC filter on, 2-FSK, no Manchester (the host decodes it, and the frame
  // markers are not Manchester anyway), sync mode from YAML.
  this->write_reg_(CC_MDMCFG2, this->sync_mode_ & 0x07);
  this->write_reg_(CC_MDMCFG1, 0x22);
  this->write_reg_(CC_MDMCFG0, 0xF8);

  // Deviation: f_xtal / 2^17 * (8 + M) * 2^E, nearest to Insteon's 75 kHz.
  // Lands on 76.17 kHz -- the same as the dongle.
  uint8_t dev_e = 0, dev_m = 0;
  double best_err = 1e12;
  for (int e = 0; e < 8; e++) {
    for (int m = 0; m < 8; m++) {
      const double d = (double) CC1101_XTAL_HZ / 131072.0 * (8 + m) * (1 << e);
      const double err = std::fabs(d - INSTEON_DEVIATION_HZ);
      if (err < best_err) {
        best_err = err;
        dev_e = e;
        dev_m = m;
        this->actual_deviation_hz_ = d;
      }
    }
  }
  this->write_reg_(CC_DEVIATN, (dev_e << 4) | dev_m);

  // Stay in RX after every capture, so the next slot is hunted for at once.
  // Autocalibrate on IDLE -> RX.
  this->write_reg_(CC_MCSM2, 0x07);
  this->write_reg_(CC_MCSM1, 0x0C);
  this->write_reg_(CC_MCSM0, 0x18);

  // The dongle's loops and AGC, verbatim.
  this->write_reg_(CC_FOCCFG, 0x17);
  this->write_reg_(CC_BSCFG, 0x6E);
  this->write_reg_(CC_AGCCTRL2, ((this->max_lna_gain_ & 0x07) << 3) | 0x03);
  this->write_reg_(CC_AGCCTRL1, 0x40);
  this->write_reg_(CC_AGCCTRL0, 0x91);

  // Front end: SmartRF's low-data-rate RX current settings; PA unused.
  this->write_reg_(CC_FREND1, 0x56);
  this->write_reg_(CC_FREND0, 0x10);
  this->write_reg_(CC_FSCAL3, 0xE9);
  this->write_reg_(CC_FSCAL2, 0x2A);
  this->write_reg_(CC_FSCAL1, 0x00);
  this->write_reg_(CC_FSCAL0, 0x1F);
  // Sensitivity-optimised TEST settings for low data rates (the dongle runs
  // the same), and VCO selection calibration for this band.
  this->write_reg_(CC_TEST2, 0x81);
  this->write_reg_(CC_TEST1, 0x35);
  this->write_reg_(CC_TEST0, 0x09);

  this->strobe_(CC_SCAL);
  delay(2);
  return true;
}

void InsteonRFCC1101::restart_rx_() {
  this->strobe_(CC_SIDLE);
  // SIDLE takes effect within a few us; wait for it rather than guess.
  for (uint8_t i = 0; i < 20 && (this->read_status_(CC_MARCSTATE) & 0x1F) != CC_MARC_IDLE; i++)
    delayMicroseconds(50);
  this->strobe_(CC_SFRX);
  this->strobe_(CC_SRX);  // FS_AUTOCAL recalibrates on the way
  this->got_ = 0;
  this->last_seen_ = 0;
}

// --------------------------------------------------------------------- radio task

void InsteonRFCC1101::task_entry_(void *arg) { static_cast<InsteonRFCC1101 *>(arg)->task_loop_(); }

void InsteonRFCC1101::task_loop_() {
  const bool have_gdo = this->gdo0_pin_ != nullptr && this->gdo2_pin_ != nullptr;
  for (;;) {
    const uint32_t now_ms = millis();
    bool want = !have_gdo;
    if (have_gdo) {
      // GDO2 high: at least 8 bytes waiting. GDO0 low with a capture under
      // way: it has ended and its tail (< 8 bytes) is waiting.
      want = this->gdo2_pin_->digital_read() || (this->got_ > 0 && !this->gdo0_pin_->digital_read());
      if (now_ms - this->last_drain_ms_ >= SPI_FALLBACK_POLL_MS)
        want = true;
    }
    if (want) {
      this->drain_();
      this->last_drain_ms_ = now_ms;
    }
    if (now_ms - this->last_health_ms_ >= HEALTH_MS) {
      this->health_check_();
      this->last_health_ms_ = now_ms;
    }
    vTaskDelay(pdMS_TO_TICKS(have_gdo ? 1 : SPI_POLL_MS));
  }
}

void InsteonRFCC1101::drain_() {
  const uint32_t now = micros();
  const uint8_t rxbytes = this->read_rxbytes_();
  if (rxbytes & 0x80) {
    // Overflow: the chip stops receiving and the byte alignment of
    // everything in the FIFO is lost. Start over.
    this->overflows_++;
    this->restart_rx_();
    return;
  }
  uint8_t avail = rxbytes & 0x7F;

  // Bytes of the current capture seen so far, read or still waiting. A
  // packet streams a byte every 877 us, so if this stops growing for
  // STALL_US the chip has abandoned the capture. (Checking for an empty FIFO
  // instead would never fire: the errata rule below always leaves a byte.)
  const uint32_t seen = (uint32_t) this->got_ + avail;
  if (seen != this->last_seen_) {
    this->last_seen_ = seen;
    this->last_progress_us_ = now;
  } else if (seen > 0 && now - this->last_progress_us_ > STALL_US) {
    // What is in hand is not worth publishing, and the stream is no longer
    // aligned to capture boundaries.
    this->stalls_++;
    this->restart_rx_();
    return;
  }

  while (avail > 0) {
    if (this->got_ == 0) {
      // A new capture. Its first byte is at the head of the FIFO, and the
      // newest byte in the FIFO finished arriving just now, so the first
      // bit was on the air `avail` byte-times ago (to within the byte
      // still in the shift register: < 0.9 ms). Sample the packet's
      // signal now, while it is still on the air: RSSI is live and the
      // offset estimate is the one the demodulator locked to.
      this->cur_.first_bit_us = now - air_us(avail);
      this->cur_.rssi = rssi_dbm_(this->read_status_(CC_RSSI));
      this->cur_.freqest = (int8_t) this->read_status_(CC_FREQEST);
    }
    const uint8_t remaining = this->capture_bytes_ - this->got_;
    uint8_t n;
    if (avail >= remaining) {
      n = remaining;  // the rest of this capture is all in
    } else {
      // Errata (SWRZ020): never read the RX FIFO empty while a packet is
      // still arriving, or the byte being written can be lost or doubled.
      n = avail - 1;
      if (n == 0)
        break;
    }
    this->read_fifo_(this->cur_.data + this->got_, n);
    this->got_ += n;
    avail -= n;
    if (this->got_ == this->capture_bytes_) {
      this->cur_.len = this->capture_bytes_;
      if (xQueueSend(this->queue_, &this->cur_, 0) != pdTRUE)
        this->queue_drops_++;
      this->got_ = 0;
    }
  }
  this->last_seen_ = (uint32_t) this->got_ + avail;
}

void InsteonRFCC1101::health_check_() {
  const uint8_t state = this->read_status_(CC_MARCSTATE) & 0x1F;
  if (state == CC_MARC_RXFIFO_OVERFLOW) {
    this->overflows_++;
    this->restart_rx_();
    return;
  }
  // RX, its end-of-packet states, and the calibration/settling states on the
  // way into RX (0x03-0x0C) are all fine. Anything else twice running --
  // IDLE above all -- means the chip has stopped listening.
  const bool fine = state == CC_MARC_RX || state == CC_MARC_RX_END || state == CC_MARC_RX_RST ||
                    (state >= 0x03 && state <= 0x0C);
  if (!fine) {
    if (++this->bad_state_checks_ >= 2) {
      this->last_bad_state_ = state;
      this->recoveries_++;
      this->restart_rx_();
      this->bad_state_checks_ = 0;
    }
    return;
  }
  this->bad_state_checks_ = 0;

  // Recalibrate between packets now and then. Costs about a millisecond of
  // deafness, and only when nothing is arriving.
  const uint32_t now_ms = millis();
  if (now_ms - this->last_recal_ms_ >= RECAL_MS && this->got_ == 0 &&
      (this->gdo0_pin_ == nullptr || !this->gdo0_pin_->digital_read()) && (this->read_rxbytes_() & 0x7F) == 0) {
    this->restart_rx_();
    this->last_recal_ms_ = now_ms;
  }
}

// --------------------------------------------------------------------- loop

void InsteonRFCC1101::loop() {
  if (!this->radio_ok_)
    return;
  this->tick_minute_();

  // Fold the task's counters in, and say what happened. Each is a capture
  // (or the start of one) that never reached the gate.
  const uint32_t overflows = this->overflows_.load();
  const uint32_t drops = this->queue_drops_.load();
  const uint32_t stalls = this->stalls_.load();
  const uint32_t recoveries = this->recoveries_.load();
  if (overflows != this->overflows_seen_) {
    ESP_LOGW(TAG, "RX FIFO overflow (%u total): the radio task was held off the bus too long",
             (unsigned) overflows);
    this->count_lost_(overflows - this->overflows_seen_);
    this->overflows_seen_ = overflows;
  }
  if (drops != this->queue_drops_seen_) {
    ESP_LOGW(TAG, "capture queue full (%u dropped total): loop() is not keeping up", (unsigned) drops);
    this->count_lost_(drops - this->queue_drops_seen_);
    this->queue_drops_seen_ = drops;
  }
  if (stalls != this->stalls_seen_) {
    ESP_LOGW(TAG, "capture stalled part-way (%u total)", (unsigned) stalls);
    this->count_lost_(stalls - this->stalls_seen_);
    this->stalls_seen_ = stalls;
  }
  if (recoveries != this->recoveries_seen_) {
    ESP_LOGW(TAG, "radio left RX (MARCSTATE 0x%02X); re-armed (%u total)", (unsigned) this->last_bad_state_.load(),
             (unsigned) recoveries);
    this->recoveries_seen_ = recoveries;
  }

  // FREQEST is in steps of f_xtal / 2^14 = 1.587 kHz.
  static const float FREQEST_KHZ = (float) CC1101_XTAL_HZ / 16384.0f / 1000.0f;
  while (xQueueReceive(this->queue_, &this->out_, 0) == pdTRUE) {
    this->process_capture_(this->out_.data, this->out_.len, this->out_.rssi, this->out_.first_bit_us,
                           this->out_.freqest * FREQEST_KHZ);
  }
}

void InsteonRFCC1101::dump_config() {
  static const char *const SYNC_MODES[] = {"none", "15/16", "16/16", "30/32",
                                           "carrier only", "15/16 + carrier", "16/16 + carrier",
                                           "30/32 + carrier"};
  ESP_LOGCONFIG(TAG, "Insteon RF listener, CC1101 (receive only):");
  ESP_LOGCONFIG(TAG, "  Chip version: 0x%02X", this->chip_version_);
  ESP_LOGCONFIG(TAG, "  Frequency: %.3f MHz", this->frequency_hz_ / 1e6f);
  ESP_LOGCONFIG(TAG, "  Bitrate: %.1f baud, deviation %.1f kHz, RX bandwidth %.1f kHz", this->actual_bitrate_,
                this->actual_deviation_hz_ / 1000.0f, this->actual_bandwidth_hz_ / 1000.0f);
  ESP_LOGCONFIG(TAG, "  Sync word: 0x%04X, %s%s", (unsigned) this->sync_word_, SYNC_MODES[this->sync_mode_ & 0x07],
                this->sync_word_ == CC1101_SYNC_WORD ? "" : "  [non-default]");
  ESP_LOGCONFIG(TAG, "  RX attenuation: %u dB, LNA gain reduction step %u", (unsigned) this->rx_attenuation_ * 6,
                (unsigned) this->max_lna_gain_);
  LOG_PIN("  CS Pin: ", this->cs_);
  LOG_PIN("  GDO0 Pin: ", this->gdo0_pin_);
  LOG_PIN("  GDO2 Pin: ", this->gdo2_pin_);
  LOG_PIN("  SW0 Pin: ", this->sw0_pin_);
  LOG_PIN("  SW1 Pin: ", this->sw1_pin_);
  this->dump_common_config_();
}

}  // namespace insteon_rf
}  // namespace esphome
