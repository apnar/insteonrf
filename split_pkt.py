#!/usr/bin/env python3
"""Compatibility wrapper: same as `insteon-rf dump`."""
import sys
from insteonrf.cli import dump_main

sys.exit(dump_main())
