#!/usr/bin/env python3
"""Compatibility wrapper: same as `insteon-rf modulate`."""
import sys

from insteonrf.cli import modulate_main

sys.exit(modulate_main())
