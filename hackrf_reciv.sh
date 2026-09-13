#!/bin/sh
# Receive Insteon RF with a HackRF and print bit strings.
# Thin wrapper kept for compatibility; equivalent to:
#
#   insteon-rf recv --backend hackrf
#
# To decode:  ./hackrf_reciv.sh | insteon-rf print

freq=${FREQ:-914950000}
sample_rate=${SAMPLE_RATE:-2400000}
gain=${RF_GAIN:-16}

exec insteon-rf recv --backend hackrf -f "${freq}" -s "${sample_rate}" --gain "${gain}" "$@"
