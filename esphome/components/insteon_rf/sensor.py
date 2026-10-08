"""Health sensors for the Insteon RF listener.

``captures_per_minute`` versus ``accepted_per_minute`` is the diagnostic that
matters: the ratio between them is the Manchester gate's rejection rate, which
is the main tuning knob and the first thing to look at when a board goes
quiet or goes noisy.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    DEVICE_CLASS_FREQUENCY,
    DEVICE_CLASS_SIGNAL_STRENGTH,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    UNIT_DECIBEL_MILLIWATT,
)

from esphome.components import sensor

from . import InsteonRF

DEPENDENCIES = ["insteon_rf"]

CONF_INSTEON_RF_ID = "insteon_rf_id"
CONF_LAST_RSSI = "last_rssi"
CONF_CAPTURES_PER_MINUTE = "captures_per_minute"
CONF_ACCEPTED_PER_MINUTE = "accepted_per_minute"
CONF_LOST_PER_MINUTE = "lost_per_minute"
CONF_FREQUENCY_OFFSET = "frequency_offset"

_COUNT_SCHEMA = sensor.sensor_schema(
    accuracy_decimals=0,
    state_class=STATE_CLASS_MEASUREMENT,
    entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_INSTEON_RF_ID): cv.use_id(InsteonRF),
        cv.Optional(CONF_LAST_RSSI): sensor.sensor_schema(
            unit_of_measurement=UNIT_DECIBEL_MILLIWATT,
            accuracy_decimals=1,
            device_class=DEVICE_CLASS_SIGNAL_STRENGTH,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_CAPTURES_PER_MINUTE): _COUNT_SCHEMA,
        cv.Optional(CONF_ACCEPTED_PER_MINUTE): _COUNT_SCHEMA,
        # Captures the radio lost before the gate saw them (CC1101: FIFO
        # overflows, stalled captures, a full hand-off queue). Nonzero means
        # the receive path is being starved, not that the air is quiet.
        cv.Optional(CONF_LOST_PER_MINUTE): _COUNT_SCHEMA,
        # CC1101 only: the demodulator's carrier-offset estimate, averaged
        # over the minute's accepted packets. A steady value is this board's
        # crystal error; take it out of `frequency:` and it should read ~0.
        cv.Optional(CONF_FREQUENCY_OFFSET): sensor.sensor_schema(
            unit_of_measurement="kHz",
            accuracy_decimals=1,
            device_class=DEVICE_CLASS_FREQUENCY,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
    }
)


async def to_code(config):
    parent = await cg.get_variable(config[CONF_INSTEON_RF_ID])
    for key, setter in (
        (CONF_LAST_RSSI, parent.set_last_rssi_sensor),
        (CONF_CAPTURES_PER_MINUTE, parent.set_captures_sensor),
        (CONF_ACCEPTED_PER_MINUTE, parent.set_accepted_sensor),
        (CONF_LOST_PER_MINUTE, parent.set_lost_sensor),
        (CONF_FREQUENCY_OFFSET, parent.set_frequency_offset_sensor),
    ):
        if key in config:
            sens = await sensor.new_sensor(config[key])
            cg.add(setter(sens))
