"""Insteon 915 MHz RF listener for the SX1262 (Heltec LoRa 32 V3).

Receive only, permanently: an Insteon network has exactly one transmitter and
it is the PLM. See Doc/MESH-PLAN.md for why this exists and how the captures
are consumed.
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
InsteonRF = insteon_rf_ns.class_("InsteonRF", cg.Component, spi.SPIDevice)

CONF_BUSY_PIN = "busy_pin"
CONF_DIO1_PIN = "dio1_pin"
CONF_CAPTURE_BYTES = "capture_bytes"
CONF_RSSI_FLOOR = "rssi_floor"
CONF_MANCHESTER_GATE = "manchester_gate"
CONF_TCXO_VOLTAGE = "tcxo_voltage"
CONF_TCXO_DELAY = "tcxo_delay"
CONF_MQTT_TOPIC = "mqtt_topic"

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

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(InsteonRF),
            cv.Required(CONF_RESET_PIN): pins.gpio_output_pin_schema,
            cv.Required(CONF_BUSY_PIN): pins.gpio_input_pin_schema,
            cv.Optional(CONF_DIO1_PIN): pins.gpio_input_pin_schema,
            # Insteon RF in the US. Not a free choice: it must match the
            # network, so it is validated to a narrow band rather than left
            # open to a typo that produces silence.
            cv.Optional(CONF_FREQUENCY, default="914.95MHz"): cv.All(
                cv.frequency, cv.Range(min=902e6, max=928e6)
            ),
            # 128 bytes is 112 ms of air: longer than a standard packet
            # (40 ms) or an extended one (98 ms), so a capture usually holds
            # a packet plus the start of the next hop repeat. The host finds
            # every packet in the block, so that is a feature.
            cv.Optional(CONF_CAPTURE_BYTES, default=128): cv.int_range(min=32, max=255),
            cv.Optional(CONF_RSSI_FLOOR, default=-110.0): cv.float_range(
                min=-130.0, max=0.0
            ),
            # Frames of Manchester-pair validity required before a capture is
            # published. This is what makes running with the preamble
            # detector off viable in a crowded 915 MHz band.
            cv.Optional(CONF_MANCHESTER_GATE, default=4): cv.int_range(min=1, max=13),
            # Getting this wrong on a Heltec V3 gives a radio that never syncs
            # and never errors, which looks exactly like a quiet house.
            cv.Optional(CONF_TCXO_VOLTAGE, default="1.8V"): cv.enum(
                TCXO_VOLTAGES, upper=True
            ),
            cv.Optional(
                CONF_TCXO_DELAY, default="5ms"
            ): cv.positive_time_period_microseconds,
            cv.Required(CONF_MQTT_TOPIC): cv.publish_topic,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(spi.spi_device_schema(cs_pin_required=True))
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await spi.register_spi_device(var, config)

    reset = await cg.gpio_pin_expression(config[CONF_RESET_PIN])
    cg.add(var.set_reset_pin(reset))
    busy = await cg.gpio_pin_expression(config[CONF_BUSY_PIN])
    cg.add(var.set_busy_pin(busy))
    if CONF_DIO1_PIN in config:
        dio1 = await cg.gpio_pin_expression(config[CONF_DIO1_PIN])
        cg.add(var.set_dio1_pin(dio1))

    cg.add(var.set_frequency(int(config[CONF_FREQUENCY])))
    cg.add(var.set_capture_bytes(config[CONF_CAPTURE_BYTES]))
    cg.add(var.set_rssi_floor(config[CONF_RSSI_FLOOR]))
    cg.add(var.set_manchester_gate(config[CONF_MANCHESTER_GATE]))
    cg.add(var.set_tcxo_voltage(config[CONF_TCXO_VOLTAGE]))
    cg.add(var.set_tcxo_delay_us(int(config[CONF_TCXO_DELAY].total_microseconds)))
    cg.add(var.set_mqtt_topic(config[CONF_MQTT_TOPIC]))
