# Src #

C helpers for the SDR receive path. They are the *fast* path only — since 2.1.0
`insteonrf/dsp.py` does the same jobs in numpy, so nothing here is required.
Built with `make` (`-Wall -Wextra -Werror`).

    fsk2_demod.c            FSK2 demodulator: raw 8-bit I/Q in, bit strings out
    fxpt_atan2.c            fast fixed-point atan2() used by the demodulator
    rf_clip.c               splits an incoming signal into one file per packet
    insteon_lib.c           older common functions; not built, kept for reference

`fsk2_demod` prints on-air bit polarity (a data `1` is the higher frequency).
Use `-U` for signed I/Q (HackRF, and the captures in `Dat/`), `-u` for unsigned
(rtl-sdr), `-s` for a different sample rate and `-b` for a different baud rate.
