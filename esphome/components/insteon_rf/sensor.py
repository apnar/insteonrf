"""Health sensors for the Insteon RF listener.

``captures_per_minute`` versus ``accepted_per_minute`` is the diagnostic that
matters: the ratio between them is the Manchester gate's rejection rate, which
is the main tuning knob and the first thing to look at when a board goes
quiet or goes noisy.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
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
    }
)


async def to_code(config):
    parent = await cg.get_variable(config[CONF_INSTEON_RF_ID])
    for key, setter in (
        (CONF_LAST_RSSI, parent.set_last_rssi_sensor),
        (CONF_CAPTURES_PER_MINUTE, parent.set_captures_sensor),
        (CONF_ACCEPTED_PER_MINUTE, parent.set_accepted_sensor),
    ):
        if key in config:
            sens = await sensor.new_sensor(config[key])
            cg.add(setter(sens))
