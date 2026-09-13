#!/bin/sh
# Transmit Insteon packets with a HackRF.
#
# Reads bit strings (from `insteon-rf pkt`) on stdin, modulates them with
# `insteon-rf modulate` into a temp file and hands that to hackrf_transfer.
# Superseded by:  insteon-rf send --backend hackrf
#
#   insteon-rf pkt -s 2B.93.07 -d 29.4E.52 0F 00 | ./hackrf_xmit.sh

set -e

freq=${FREQ:-914950000}
sample_rate=${SAMPLE_RATE:-2400000}
tx_gain=${TX_GAIN:-20}

iq=$(mktemp -t insteonrf-XXXXXX.iq)
trap 'rm -f "$iq"' EXIT

insteon-rf modulate -s "${sample_rate}" -o "$iq"
ls -l "$iq"

hackrf_transfer -x "${tx_gain}" -a 1 -s "${sample_rate}" -f "${freq}" -t "$iq"
