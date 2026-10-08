// Insteon RF listener on the CC1101 (LilyGO T-Embed CC1101 / CC1101 Plus).
//
// The CC1101 is the radio half of the CC1111 -- the chip in the rfcat dongle,
// which is the best receiver on this network. So this driver does not have
// to invent a configuration: it copies the dongle's modem registers (read
// live from the insteonrf pod's "radio at start" dump on 2026-10-08), only
// rescaled from the CC1111's 24 MHz crystal to the CC1101's 26 MHz:
//
//   2-FSK, 9124 baud, 76.2 kHz deviation, sync 0x3155 16/16 + carrier sense,
//   PQT 0, no CRC, no whitening, no Manchester, fixed length,
//   FOCCFG 0x17, BSCFG 0x6E, AGCCTRL 0x03/0x40/0x91, TEST 0x81/0x35/0x09.
//
// What differs from the dongle is everything around the modem. The chip
// has a 64-byte RX FIFO, and a capture is 162 bytes, so it has to be drained
// *while* the packet arrives: 64 bytes is 56 ms of air. ESPHome's own cc1101
// component reads a packet only once it has ended, which caps it at 64
// bytes; that is why this is a separate driver.
//
// The FIFO is drained by a FreeRTOS task of its own rather than by loop().
// On this board the display, the SD slot, the nRF24 and the CC1101 share one
// SPI bus, and anything that holds loop() up -- a display refresh, an MQTT
// reconnect, WiFi -- for longer than the FIFO lasts would overflow it. That
// is the dongle's failure mode exactly (its firmware drops a packet when the
// host has not shipped the previous one), and the fix is the same as the
// pod's: nothing heavy on the receive thread. The task only moves bytes and
// stamps them; gating and publishing happen in loop(), off a queue.
//
// LilyGO T-Embed CC1101 pins, from LilyGO's examples/utilities.h and
// docs/pinmap_cn.md (hardware v1.0-240729/241103): SPI SCK 11, MOSI 9,
// MISO 10 (shared with LCD CS 41, SD CS 13, nRF24 CS 44); CC1101 CS 12,
// GDO0 3, GDO2 38; RF band switch SW1 47, SW0 48 (SW1=0 SW0=1 selects the
// 868/915 MHz filter); BOARD_PWR_EN 15 powers the CC1101 (and the LEDs).

#pragma once

#include "insteon_rf.h"
#include "esphome/components/spi/spi.h"

#include <atomic>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

namespace esphome {
namespace insteon_rf {

// The default 16-bit sync word: the start header, inverted, exactly what the
// CC1111 dongle syncs on. Pinned to the low half of INSTEON_SYNC_WORD by
// tests/test_sync_word.py.
static const uint16_t CC1101_SYNC_WORD = 0x3155;
static const uint32_t CC1101_XTAL_HZ = 26000000;

// Configuration registers.
enum : uint8_t {
  CC_IOCFG2 = 0x00,
  CC_IOCFG1 = 0x01,
  CC_IOCFG0 = 0x02,
  CC_FIFOTHR = 0x03,
  CC_SYNC1 = 0x04,
  CC_SYNC0 = 0x05,
  CC_PKTLEN = 0x06,
  CC_PKTCTRL1 = 0x07,
  CC_PKTCTRL0 = 0x08,
  CC_ADDR = 0x09,
  CC_CHANNR = 0x0A,
  CC_FSCTRL1 = 0x0B,
  CC_FSCTRL0 = 0x0C,
  CC_FREQ2 = 0x0D,
  CC_FREQ1 = 0x0E,
  CC_FREQ0 = 0x0F,
  CC_MDMCFG4 = 0x10,
  CC_MDMCFG3 = 0x11,
  CC_MDMCFG2 = 0x12,
  CC_MDMCFG1 = 0x13,
  CC_MDMCFG0 = 0x14,
  CC_DEVIATN = 0x15,
  CC_MCSM2 = 0x16,
  CC_MCSM1 = 0x17,
  CC_MCSM0 = 0x18,
  CC_FOCCFG = 0x19,
  CC_BSCFG = 0x1A,
  CC_AGCCTRL2 = 0x1B,
  CC_AGCCTRL1 = 0x1C,
  CC_AGCCTRL0 = 0x1D,
  CC_FREND1 = 0x21,
  CC_FREND0 = 0x22,
  CC_FSCAL3 = 0x23,
  CC_FSCAL2 = 0x24,
  CC_FSCAL1 = 0x25,
  CC_FSCAL0 = 0x26,
  CC_TEST2 = 0x2C,
  CC_TEST1 = 0x2D,
  CC_TEST0 = 0x2E,
};

// Command strobes.
enum : uint8_t {
  CC_SRES = 0x30,
  CC_SCAL = 0x33,
  CC_SRX = 0x34,
  CC_SIDLE = 0x36,
  CC_SFRX = 0x3A,
  CC_SNOP = 0x3D,
};

// Status registers; read with the burst bit set (0xC0 | addr).
enum : uint8_t {
  CC_PARTNUM = 0x30,
  CC_VERSION = 0x31,
  CC_FREQEST = 0x32,
  CC_LQI = 0x33,
  CC_RSSI = 0x34,
  CC_MARCSTATE = 0x35,
  CC_RXBYTES = 0x3B,
};

static const uint8_t CC_FIFO = 0x3F;
static const uint8_t CC_READ = 0x80;
static const uint8_t CC_BURST = 0x40;

// MARCSTATE values that matter here.
enum : uint8_t {
  CC_MARC_IDLE = 0x01,
  CC_MARC_RX = 0x0D,
  CC_MARC_RX_END = 0x0E,
  CC_MARC_RX_RST = 0x0F,
  CC_MARC_RXFIFO_OVERFLOW = 0x11,
};

/// One capture handed from the radio task to loop().
struct CC1101Capture {
  uint32_t first_bit_us;
  float rssi;
  int8_t freqest;
  uint8_t len;
  uint8_t data[255];
};

class InsteonRFCC1101 : public InsteonRF,
                        public spi::SPIDevice<spi::BIT_ORDER_MSB_FIRST, spi::CLOCK_POLARITY_LOW,
                                              spi::CLOCK_PHASE_LEADING, spi::DATA_RATE_4MHZ> {
 public:
  InsteonRFCC1101() { this->sync_word_ = CC1101_SYNC_WORD; }

  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_gdo0_pin(GPIOPin *pin) { this->gdo0_pin_ = pin; }
  void set_gdo2_pin(GPIOPin *pin) { this->gdo2_pin_ = pin; }
  void set_sw0_pin(GPIOPin *pin) { this->sw0_pin_ = pin; }
  void set_sw1_pin(GPIOPin *pin) { this->sw1_pin_ = pin; }
  void set_frequency(uint32_t hz) { this->frequency_hz_ = hz; }
  void set_bandwidth(uint32_t hz) { this->bandwidth_hz_ = hz; }
  void set_sync_mode(uint8_t mode) { this->sync_mode_ = mode; }
  void set_rx_attenuation(uint8_t code) { this->rx_attenuation_ = code; }
  void set_max_lna_gain(uint8_t code) { this->max_lna_gain_ = code; }
  void set_carrier_sense_abs(int8_t thr) { this->carrier_sense_abs_ = thr; }

  // -- live tuning, from loop() (template numbers/selects in YAML). The
  // values are staged here and the radio task reprograms the chip between
  // captures, so an A/B test needs no reflash.
  void tune_frequency(uint32_t hz) { this->frequency_hz_ = hz; this->request_reconfig_(); }
  void tune_bandwidth(uint32_t hz) { this->bandwidth_hz_ = hz; this->request_reconfig_(); }
  void tune_sync_mode(uint8_t mode) { this->sync_mode_ = mode; this->request_reconfig_(); }
  void tune_rx_attenuation(uint8_t code) { this->rx_attenuation_ = code; this->request_reconfig_(); }
  void tune_max_lna_gain(uint8_t code) { this->max_lna_gain_ = code; this->request_reconfig_(); }
  void tune_carrier_sense_abs(int8_t thr) { this->carrier_sense_abs_ = thr; this->request_reconfig_(); }
  void tune_sync_word(uint16_t word) { this->sync_word_ = word; this->request_reconfig_(); }
  /// Drive the band switch directly: 0 = from the frequency (normal),
  /// 1 = 315 MHz path, 2 = 433 MHz path, 3 = 868/915 MHz path.
  void tune_band(uint8_t band) { this->band_override_ = band; this->request_reconfig_(); }
  /// Sweep [start, stop] in `step` Hz, dwelling `dwell_ms` per step, and log
  /// the peak RSSI per step at INFO. Generate traffic while it runs.
  void request_scan(uint32_t start, uint32_t stop, uint32_t step, uint32_t dwell_ms) {
    this->scan_start_ = start;
    this->scan_stop_ = stop;
    this->scan_step_ = step;
    this->scan_dwell_ms_ = dwell_ms;
    this->scan_requested_ = true;
  }
  /// Override one configuration register (0x00-0x2E) on top of the
  /// computed configuration, until reboot; addr 0xFF clears all overrides.
  void tune_register(uint8_t addr, uint8_t value) {
    if (addr == 0xFF) {
      this->overrides_n_ = 0;
    } else if (addr <= 0x2E) {
      uint8_t i = 0;
      while (i < this->overrides_n_ && this->overrides_[i][0] != addr)
        i++;
      if (i < sizeof(this->overrides_) / sizeof(this->overrides_[0])) {
        this->overrides_[i][0] = addr;
        this->overrides_[i][1] = value;
        if (i == this->overrides_n_)
          this->overrides_n_++;
      }
    }
    this->request_reconfig_();
  }
  uint32_t get_frequency() const { return this->frequency_hz_; }
  uint32_t get_bandwidth() const { return this->bandwidth_hz_; }
  uint8_t get_sync_mode() const { return this->sync_mode_; }

 protected:
  // -- SPI plumbing. After setup() only the radio task calls these.
  uint8_t strobe_(uint8_t cmd);
  void write_reg_(uint8_t addr, uint8_t value);
  uint8_t read_reg_(uint8_t addr);
  uint8_t read_status_(uint8_t addr);
  uint8_t read_rxbytes_();
  void read_fifo_(uint8_t *out, size_t n);

  bool configure_radio_();
  void request_reconfig_() { this->reconfig_requested_ = true; }
  bool verify_registers_();
  void apply_band_switch_();
  void program_frequency_(uint32_t hz);
  void run_scan_();
  void restart_rx_();
  static float rssi_dbm_(uint8_t raw) { return (float) (int8_t) raw / 2.0f - 74.0f; }

  // -- the radio task
  static void task_entry_(void *arg);
  void task_loop_();
  void drain_();
  void health_check_();

  GPIOPin *gdo0_pin_{nullptr};
  GPIOPin *gdo2_pin_{nullptr};
  GPIOPin *sw0_pin_{nullptr};
  GPIOPin *sw1_pin_{nullptr};

  uint32_t frequency_hz_{914990000};
  uint32_t bandwidth_hz_{203000};
  uint8_t sync_mode_{6};  // 16/16 + carrier sense, as the dongle
  uint8_t rx_attenuation_{0};
  uint8_t max_lna_gain_{0};
  // AGCCTRL1.CARRIER_SENSE_ABS_THR, signed dB relative to MAGN_TARGET;
  // -8 disables the absolute threshold. 0 is the dongle's setting.
  int8_t carrier_sense_abs_{0};

  // What configure_radio_() actually programmed, for dump_config().
  float actual_bitrate_{0};
  float actual_deviation_hz_{0};
  float actual_bandwidth_hz_{0};
  uint8_t chip_version_{0};

  QueueHandle_t queue_{nullptr};
  TaskHandle_t task_{nullptr};

  // Task-owned state: the capture being assembled.
  CC1101Capture cur_{};
  uint8_t got_{0};
  uint32_t last_progress_us_{0};
  uint32_t last_seen_{0};
  uint32_t last_drain_ms_{0};
  uint32_t last_health_ms_{0};
  uint32_t last_recal_ms_{0};
  uint8_t bad_state_checks_{0};

  // Written by the task, read by loop().
  std::atomic<uint32_t> overflows_{0};
  std::atomic<uint32_t> queue_drops_{0};
  std::atomic<uint32_t> stalls_{0};
  std::atomic<uint32_t> recoveries_{0};
  std::atomic<uint8_t> last_bad_state_{0};
  std::atomic<uint32_t> syncs_{0};        // GDO0 rising edges: sync words matched
  std::atomic<uint32_t> reconfigs_{0};    // chip found with lost settings, reprogrammed
  std::atomic<uint8_t> diag_state_{0};    // MARCSTATE at the last health check
  std::atomic<uint8_t> diag_rssi_{0};     // raw RSSI at the last health check
  std::atomic<uint8_t> diag_rxbytes_{0};
  std::atomic<bool> reconfig_requested_{false};
  std::atomic<bool> scan_requested_{false};
  std::atomic<bool> hard_reset_requested_{false};
  std::atomic<uint32_t> hard_resets_{0};
  uint8_t deaf_minutes_{0};
  uint32_t accepted_at_diag_{0};
  std::atomic<uint8_t> diag_rssi_max_{0x80};  // peak raw RSSI since the last status line
  std::atomic<bool> diag_rssi_reset_{false};
  uint8_t band_override_{0};
  uint8_t overrides_[12][2]{};
  uint8_t overrides_n_{0};
  uint32_t scan_start_{0}, scan_stop_{0}, scan_step_{0}, scan_dwell_ms_{0};
  bool gdo0_was_high_{false};
  uint32_t last_verify_ms_{0};
  uint32_t last_diag_log_ms_{0};
  uint32_t syncs_at_diag_{0};
  uint32_t captures_at_diag_{0};
  // loop()'s view of the above, to report changes.
  uint32_t overflows_seen_{0};
  uint32_t queue_drops_seen_{0};
  uint32_t stalls_seen_{0};
  uint32_t recoveries_seen_{0};

  CC1101Capture out_{};  // loop()'s receive buffer
};

}  // namespace insteon_rf
}  // namespace esphome
