#!/bin/sh
# Same as hackrf_xmit.sh but streams the samples straight into hackrf_transfer.
#
# NOTE: upstream hackrf_transfer cannot read from stdin — this needs the
# patched build. Use hackrf_xmit.sh (temp file) with an unpatched binary.

set -e

freq=${FREQ:-914950000}
sample_rate=${SAMPLE_RATE:-2400000}
tx_gain=${TX_GAIN:-20}

insteon-rf modulate -s "${sample_rate}" \
    | hackrf_transfer -x "${tx_gain}" -a 1 -s "${sample_rate}" -f "${freq}" -t -
