#!/bin/sh
# Receive Insteon RF with an rtl-sdr and print bit strings.
# Thin wrapper kept for compatibility; equivalent to:
#
#   insteon-rf recv --backend rtlsdr
#
# To decode:  ./rtl_reciv.sh | insteon-rf print

freq=${FREQ:-914950000}
sample_rate=${SAMPLE_RATE:-2400000}
gain=${RF_GAIN:-19.9}

exec insteon-rf recv --backend rtlsdr -f "${freq}" -s "${sample_rate}" --gain "${gain}" "$@"
