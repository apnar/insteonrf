"""Insteon 915 MHz RF listener for ESPHome boards.

Receive only, permanently: an Insteon network has exactly one transmitter and
it is the PLM. See Doc/MESH-PLAN.md for why this exists and how the captures
are consumed.

Two radios, chosen with ``radio:``:

* ``sx1262`` (the default, so existing configs need no change) -- Heltec
  LoRa 32 V3. GFSK packet mode used as a bit recorder.
* ``cc1101`` -- LilyGO T-Embed CC1101 / CC1101 Plus. The CC1111 dongle's own
  modem, configured the way the dongle is.

Both publish identical capture messages to MQTT, so the mesh cannot tell them
apart except by name.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_FREQUENCY, CONF_ID, CONF_RESET_PIN

from esphome import pins
from esphome.components import spi

CODEOWNERS = ["@apnar"]
DEPENDENCIES = ["spi"]
AUTO_LOAD = ["sensor"]
MULTI_CONF = False

insteon_rf_ns = cg.esphome_ns.namespace("insteon_rf")
InsteonRF = insteon_rf_ns.class_("InsteonRF", cg.Component)
InsteonRFSX1262 = insteon_rf_ns.class_("InsteonRFSX1262", InsteonRF, spi.SPIDevice)
InsteonRFCC1101 = insteon_rf_ns.class_("InsteonRFCC1101", InsteonRF, spi.SPIDevice)

CONF_RADIO = "radio"
CONF_BUSY_PIN = "busy_pin"
CONF_DIO1_PIN = "dio1_pin"
CONF_GDO0_PIN = "gdo0_pin"
CONF_GDO2_PIN = "gdo2_pin"
CONF_SW0_PIN = "sw0_pin"
CONF_SW1_PIN = "sw1_pin"
CONF_BANDWIDTH = "bandwidth"
CONF_SYNC_MODE = "sync_mode"
CONF_RX_ATTENUATION = "rx_attenuation"
CONF_MAX_LNA_GAIN_REDUCTION = "max_lna_gain_reduction"
CONF_CARRIER_SENSE_THRESHOLD = "carrier_sense_threshold"
CONF_IDLE_REARM = "idle_rearm"
CONF_QUIET_END = "quiet_end"
CONF_SYNC_WORD = "sync_word"
CONF_SYNC_WORD_BITS = "sync_word_bits"
CONF_PREAMBLE_DETECTOR = "preamble_detector_bits"
CONF_CAPTURE_BYTES = "capture_bytes"
CONF_RSSI_FLOOR = "rssi_floor"
CONF_MANCHESTER_GATE = "manchester_gate"
CONF_MANCHESTER_GATE_ERRORS = "manchester_gate_errors"
CONF_TCXO_VOLTAGE = "tcxo_voltage"
CONF_TCXO_DELAY = "tcxo_delay"
CONF_MQTT_TOPIC = "mqtt_topic"

# From tools/gen_sync_word.py, pinned by tests/test_sync_word.py. The
# complement (0xCCCCCEAA) is the one to try if the radio's bit sense turns
# out opposite to the CC1111's: a wrong polarity produces no captures and no
# errors, exactly like a wiring fault, so it has to be flippable over OTA.
DEFAULT_SYNC_WORD = 0x33333155
# The CC1101 matches 16 bits: the start header alone, as the dongle does.
# It is the CC1111's modem, so its bit sense is the dongle's and the
# complement (0xCEAA) should never be needed.
DEFAULT_CC1101_SYNC_WORD = 0x3155

# SX126x SetDIO3AsTcxoCtrl voltage codes.
TCXO_VOLTAGES = {
    "1.6V": 0x00,
    "1.7V": 0x01,
    "1.8V": 0x02,
    "2.2V": 0x03,
    "2.4V": 0x04,
    "2.7V": 0x05,
    "3.0V": 0x06,
    "3.3V": 0x07,
}

# SX126x GFSK PreambleDetectorLength codes. Off by default: Insteon's preamble
# is a repeating 0110 cell rather than the 0x55/0xAA alternation the detector
# expects, so it may never fire. Exposed so it can be tried on hardware.
PREAMBLE_DETECTOR = {
    0: 0x00,
    8: 0x04,
    16: 0x05,
    24: 0x06,
    32: 0x07,
}

# CC1101 MDMCFG2.SYNC_MODE. "+ carrier" also requires the RSSI to be above
# the carrier-sense threshold, which is what the dongle runs (mode 6) and
# what keeps a 16-bit word from syncing on noise all day. 15/16 tolerates
# one bit error in the start header: more syncs on weak packets, more on
# noise; the Manchester gate absorbs the latter.
CC1101_SYNC_MODES = {
    "16/16": 2,
    "15/16": 1,
    "16/16 + carrier": 6,
    "15/16 + carrier": 5,
}

# CC1101 FIFOTHR.CLOSE_IN_RX: front-end attenuation, for a board that has to
# sit next to the PLM.
CC1101_RX_ATTENUATION = {
    "0db": 0,
    "6db": 1,
    "12db": 2,
    "18db": 3,
}

_COMMON_SCHEMA = cv.Schema(
    {
        # Sized to the slot grid, not to a packet. Insteon traffic sits
        # on 456-bit (50 ms) slots: a message, its hop repeats and then
        # the ACK, back to back. The FIFO starts at the same point in
        # every slot, and the next slot's 32-bit sync word begins 424
        # bits after it -- so a capture must end just short of a sync
        # word, or the packet in that slot is cut in half and lost, and
        # the chip only resumes hunting at the slot after.
        #
        # 162 bytes = 1296 bits holds slots 0, 1 and 2 whole (the third
        # ends at bit 1265) and stops 40 bits before slot 3's sync
        # (bit 1336), so continuous RX picks that slot up by itself. The
        # old 128 (1024 bits) ended inside slot 2 -- which is exactly
        # where the ACK to a one-hop message goes. It also fits an
        # extended packet (857 bits after the sync). The same arithmetic
        # holds for the CC1101: its 16-bit word ends where the SX1262's
        # 32-bit one does, at the end of the start header.
        cv.Optional(CONF_CAPTURE_BYTES, default=162): cv.int_range(min=32, max=255),
        cv.Optional(CONF_RSSI_FLOOR, default=-110.0): cv.float_range(min=-130.0, max=0.0),
        # Frames of Manchester-pair validity required before a capture is
        # published. This is what makes running with the preamble
        # detector off viable in a crowded 915 MHz band.
        cv.Optional(CONF_MANCHESTER_GATE, default=4): cv.int_range(min=1, max=13),
        # Invalid pairs tolerated within those frames. The host repairs
        # one or two bad symbols from hard bits, so rejecting them here
        # threw away exactly the marginal packets it could have saved;
        # noise breaks ~25 of the ~50 pairs, so 2 is still airtight.
        cv.Optional(CONF_MANCHESTER_GATE_ERRORS, default=2): cv.int_range(min=0, max=8),
        cv.Required(CONF_MQTT_TOPIC): cv.publish_topic,
    }
).extend(cv.COMPONENT_SCHEMA)

SX1262_SCHEMA = (
    _COMMON_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(InsteonRFSX1262),
            cv.Required(CONF_RESET_PIN): pins.gpio_output_pin_schema,
            cv.Required(CONF_BUSY_PIN): pins.gpio_input_pin_schema,
            cv.Optional(CONF_DIO1_PIN): pins.gpio_input_pin_schema,
            # Insteon RF in the US. Not a free choice: it must match the
            # network, so it is validated to a narrow band rather than left
            # open to a typo that produces silence.
            cv.Optional(CONF_FREQUENCY, default="914.95MHz"): cv.All(
                cv.frequency, cv.Range(min=902e6, max=928e6)
            ),
            cv.Optional(CONF_SYNC_WORD, default=DEFAULT_SYNC_WORD): cv.hex_uint32_t,
            # How many of its low bits the radio matches on: 16 is the start
            # header alone, each further 8 is two more preamble cells the bit
            # synchroniser must already be locked for.
            cv.Optional(CONF_SYNC_WORD_BITS, default=32): cv.one_of(16, 24, 32, int=True),
            cv.Optional(CONF_PREAMBLE_DETECTOR, default=0): cv.enum(PREAMBLE_DETECTOR),
            # Getting this wrong on a Heltec V3 gives a radio that never syncs
            # and never errors, which looks exactly like a quiet house.
            cv.Optional(CONF_TCXO_VOLTAGE, default="1.8V"): cv.enum(TCXO_VOLTAGES, upper=True),
            cv.Optional(CONF_TCXO_DELAY, default="5ms"): cv.positive_time_period_microseconds,
        }
    )
    .extend(spi.spi_device_schema(cs_pin_required=True))
)

CC1101_SCHEMA = (
    _COMMON_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(InsteonRFCC1101),
            # Both optional: without them the radio task polls the FIFO
            # over SPI every 2 ms instead of reading two GPIOs every 1 ms.
            cv.Optional(CONF_GDO0_PIN): pins.gpio_input_pin_schema,
            cv.Optional(CONF_GDO2_PIN): pins.gpio_input_pin_schema,
            # The T-Embed's band-filter switch. Set from the frequency at
            # boot; left on the 315/433 MHz filter the radio hears nothing.
            cv.Optional(CONF_SW0_PIN): pins.gpio_output_pin_schema,
            cv.Optional(CONF_SW1_PIN): pins.gpio_output_pin_schema,
            # Where the network actually is. The V3 (1 ppm TCXO) put it at
            # 914.990 MHz, not the nominal 914.950; the dongle is tuned to
            # 914.950 only because its own crystal is that far off. Correct
            # this board by its "Frequency Offset" sensor once it has heard
            # a few hundred packets.
            cv.Optional(CONF_FREQUENCY, default="914.99MHz"): cv.All(
                cv.frequency, cv.Range(min=902e6, max=928e6)
            ),
            # Channel filter, rounded up to the next step the chip has
            # (203 kHz is the closest to the dongle's 187.5 at 26 MHz).
            cv.Optional(CONF_BANDWIDTH, default="203kHz"): cv.All(
                cv.frequency, cv.Range(min=58e3, max=812e3)
            ),
            cv.Optional(CONF_SYNC_WORD, default=DEFAULT_CC1101_SYNC_WORD): cv.hex_uint16_t,
            cv.Optional(CONF_SYNC_MODE, default="16/16 + carrier"): cv.enum(CC1101_SYNC_MODES),
            cv.Optional(CONF_RX_ATTENUATION, default="0dB"): cv.enum(
                CC1101_RX_ATTENUATION, lower=True
            ),
            # AGCCTRL2.MAX_LNA_GAIN: 0 = full LNA gain, each step backs it off.
            cv.Optional(CONF_MAX_LNA_GAIN_REDUCTION, default=0): cv.int_range(min=0, max=7),
            # AGCCTRL1.CARRIER_SENSE_ABS_THR in dB relative to MAGN_TARGET,
            # used by the "+ carrier" sync modes; -8 disables it. 0 is the
            # dongle's setting.
            cv.Optional(CONF_CARRIER_SENSE_THRESHOLD, default=0): cv.int_range(min=-8, max=7),
            # Re-enter RX after this much quiet air. Without it a burst after
            # >= 2 s of silence lost its first copy about half the time; with
            # it (500 ms or 2 s, measured 2026-10-09) about 6%. 0 disables.
            # End a capture once carrier sense has been low this long, so a
            # capture that outlived its exchange cannot hide the next one's
            # first copy. Insteon packets in one exchange are 10 ms apart.
            # Measured 2026-10-09 (back-to-back traffic, three pairs): first
            # copies 91.6% -> 93.3%, decoded 99.0% -> 99.8%. 0 disables.
            cv.Optional(CONF_QUIET_END, default="20ms"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(max=cv.TimePeriod(milliseconds=1000)),
            ),
            cv.Optional(CONF_IDLE_REARM, default="1s"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(max=cv.TimePeriod(milliseconds=60000)),
            ),
        }
    )
    .extend(spi.spi_device_schema(cs_pin_required=True))
)

CONFIG_SCHEMA = cv.typed_schema(
    {"sx1262": SX1262_SCHEMA, "cc1101": CC1101_SCHEMA},
    key=CONF_RADIO,
    default_type="sx1262",
    lower=True,
)


async def _common_to_code(var, config):
    await cg.register_component(var, config)
    await spi.register_spi_device(var, config)
    cg.add(var.set_sync_word(config[CONF_SYNC_WORD]))
    cg.add(var.set_capture_bytes(config[CONF_CAPTURE_BYTES]))
    cg.add(var.set_rssi_floor(config[CONF_RSSI_FLOOR]))
    cg.add(var.set_manchester_gate(config[CONF_MANCHESTER_GATE]))
    cg.add(var.set_manchester_gate_errors(config[CONF_MANCHESTER_GATE_ERRORS]))
    cg.add(var.set_mqtt_topic(config[CONF_MQTT_TOPIC]))
    cg.add(var.set_frequency(int(config[CONF_FREQUENCY])))


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await _common_to_code(var, config)

    if config[CONF_RADIO] == "cc1101":
        for key, setter in (
            (CONF_GDO0_PIN, var.set_gdo0_pin),
            (CONF_GDO2_PIN, var.set_gdo2_pin),
            (CONF_SW0_PIN, var.set_sw0_pin),
            (CONF_SW1_PIN, var.set_sw1_pin),
        ):
            if key in config:
                pin = await cg.gpio_pin_expression(config[key])
                cg.add(setter(pin))
        cg.add(var.set_bandwidth(int(config[CONF_BANDWIDTH])))
        cg.add(var.set_sync_mode(config[CONF_SYNC_MODE]))
        cg.add(var.set_rx_attenuation(config[CONF_RX_ATTENUATION]))
        cg.add(var.set_max_lna_gain(config[CONF_MAX_LNA_GAIN_REDUCTION]))
        cg.add(var.set_carrier_sense_abs(config[CONF_CARRIER_SENSE_THRESHOLD]))
        cg.add(var.set_idle_rearm_ms(config[CONF_IDLE_REARM].total_milliseconds))
        cg.add(var.set_quiet_end_ms(config[CONF_QUIET_END].total_milliseconds))
        return

    reset = await cg.gpio_pin_expression(config[CONF_RESET_PIN])
    cg.add(var.set_reset_pin(reset))
    busy = await cg.gpio_pin_expression(config[CONF_BUSY_PIN])
    cg.add(var.set_busy_pin(busy))
    if CONF_DIO1_PIN in config:
        dio1 = await cg.gpio_pin_expression(config[CONF_DIO1_PIN])
        cg.add(var.set_dio1_pin(dio1))
    cg.add(var.set_sync_bits(config[CONF_SYNC_WORD_BITS]))
    cg.add(var.set_preamble_detector(config[CONF_PREAMBLE_DETECTOR]))
    cg.add(var.set_tcxo_voltage(config[CONF_TCXO_VOLTAGE]))
    cg.add(var.set_tcxo_delay_us(int(config[CONF_TCXO_DELAY].total_microseconds)))
