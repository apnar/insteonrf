// Insteon RF listener on the SX1262 (Heltec LoRa 32 V3).
//
// How it works, and why it is unusual: the SX1262 has no continuous or
// raw-bitstream mode (the SX127x family exposes DATA and DCLK pins for that;
// SX126x dropped it). So GFSK packet mode is used as a raw bit recorder --
// sync word set to the invariant start of every Insteon packet, preamble
// detector off, CRC off, whitening off, address filtering off, fixed payload
// length. The packet engine is just a shift register feeding the FIFO, so it
// happily records a Manchester-coded payload with interleaved frame counters
// that it understands nothing about. The host decodes it.
//
// Insteon parameters: 914.95 MHz, 9124 baud, 75 kHz deviation, and 234.3 kHz
// receive bandwidth (the nearest step above 2*75 + 9.1 kHz).
//
// Pins verified 2026-09-19 against Meshtastic's heltec_v3 variant: SCK 9,
// MISO 11, MOSI 10, CS 8, RESET 12, BUSY 13, DIO1 14; TCXO on DIO3 at 1.8 V;
// DIO2 drives the RF switch; DC-DC regulator.

#pragma once

#include "insteon_rf.h"
#include "esphome/components/spi/spi.h"

namespace esphome {
namespace insteon_rf {

static const uint32_t SX126X_XTAL_HZ = 32000000;

// SX126x opcodes (datasheet chapter 13).
enum : uint8_t {
  SX_SET_STANDBY = 0x80,
  SX_SET_RX = 0x82,
  SX_SET_RF_FREQUENCY = 0x86,
  SX_SET_PACKET_TYPE = 0x8A,
  SX_SET_MODULATION_PARAMS = 0x8B,
  SX_SET_PACKET_PARAMS = 0x8C,
  SX_SET_BUFFER_BASE = 0x8F,
  SX_SET_DIO_IRQ_PARAMS = 0x08,
  SX_GET_IRQ_STATUS = 0x12,
  SX_GET_RX_BUFFER_STATUS = 0x13,
  SX_CLEAR_IRQ_STATUS = 0x02,
  SX_READ_BUFFER = 0x1E,
  SX_WRITE_REGISTER = 0x0D,
  SX_READ_REGISTER = 0x1D,
  SX_GET_PACKET_STATUS = 0x14,
  SX_GET_RSSI_INST = 0x15,
  SX_SET_DIO2_RF_SWITCH = 0x9D,
  SX_SET_DIO3_TCXO = 0x97,
  SX_SET_REGULATOR_MODE = 0x96,
  SX_CALIBRATE = 0x89,
  SX_CALIBRATE_IMAGE = 0x98,
  SX_GET_STATUS = 0xC0,
};

// Sync word register block, 8 bytes.
static const uint16_t SX_REG_SYNC_WORD_0 = 0x06C0;
// RX gain: 0x94 power-saving (default), 0x96 boosted. A mains-powered
// listener wants the boost.
static const uint16_t SX_REG_RX_GAIN = 0x08AC;

// GFSK receive bandwidth codes. 0x0A is 234.3 kHz.
static const uint8_t SX_GFSK_BW_234_3 = 0x0A;

// IRQ bits.
static const uint16_t SX_IRQ_RX_DONE = 0x0002;
static const uint16_t SX_IRQ_TIMEOUT = 0x0200;
static const uint16_t SX_IRQ_ALL = 0xFFFF;

class InsteonRFSX1262 : public InsteonRF,
                        public spi::SPIDevice<spi::BIT_ORDER_MSB_FIRST, spi::CLOCK_POLARITY_LOW,
                                              spi::CLOCK_PHASE_LEADING, spi::DATA_RATE_8MHZ> {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_reset_pin(GPIOPin *pin) { this->reset_pin_ = pin; }
  void set_busy_pin(GPIOPin *pin) { this->busy_pin_ = pin; }
  void set_dio1_pin(GPIOPin *pin) { this->dio1_pin_ = pin; }
  void set_frequency(uint32_t hz) { this->frequency_hz_ = hz; }
  void set_sync_bits(uint8_t bits) { this->sync_bits_ = bits; }
  void set_preamble_detector(uint8_t code) { this->preamble_detector_ = code; }
  void set_tcxo_voltage(uint8_t code) { this->tcxo_voltage_ = code; }
  void set_tcxo_delay_us(uint32_t us) { this->tcxo_delay_us_ = us; }

 protected:
  // -- SX126x plumbing
  void wait_busy_(uint32_t timeout_ms = 100);
  void cmd_(uint8_t opcode, const uint8_t *data, size_t len);
  void read_cmd_(uint8_t opcode, uint8_t *out, size_t len);
  void write_register_(uint16_t addr, const uint8_t *data, size_t len);
  void read_register_(uint16_t addr, uint8_t *out, size_t len);
  void read_buffer_(uint8_t offset, uint8_t *out, size_t len);
  bool configure_radio_();
  void start_rx_();
  float read_rssi_();
  void handle_capture_();

  GPIOPin *reset_pin_{nullptr};
  GPIOPin *busy_pin_{nullptr};
  GPIOPin *dio1_pin_{nullptr};

  uint32_t frequency_hz_{914950000};
  uint8_t sync_bits_{INSTEON_SYNC_BITS};
  uint8_t preamble_detector_{0x00};  // off; see __init__.py for the codes
  uint8_t tcxo_voltage_{0x02};       // 1.8 V; Heltec V3 drives the TCXO from DIO3
  uint32_t tcxo_delay_us_{5000};

  //: micros() when the last RxDone was seen, for stamping the capture with
  //: when it was on the air rather than when it was published.
  uint32_t rx_done_us_{0};
  //: Keeps loop() running flat out instead of every 16 ms, so RxDone is
  //: noticed within a millisecond and the timestamp does not wander by a
  //: loop period.
  HighFrequencyLoopRequester high_freq_;
  uint8_t buffer_[256]{};
};

}  // namespace insteon_rf
}  // namespace esphome
