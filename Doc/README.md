# Documentation #

[pkt_format.md](pkt_format.md)                  Description of the Insteon RF protocol:
packet fields, the 28-bit-per-byte Manchester framing, and both CRC algorithms.

[crc.txt](crc.txt)                              How the packet CRC was reverse-engineered
([blog post](http://make-it-hack.blogspot.com/2015/08/reverse-engineering-crc.html)).

[usb-notes.md](usb-notes.md)                    The rfcat USB "wedge": how it was
reproduced, why rflib's `cleanup()` was the cause, and what the fix does.

[insteon_defcon23.pdf](insteon_defcon23.pdf)    Slides from DEF CON 23.
