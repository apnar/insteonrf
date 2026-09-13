#!/usr/bin/env python3
"""Compatibility wrapper: same as `insteon-rf pkt`."""
import sys

from insteonrf.cli import pkt_main

sys.exit(pkt_main())
