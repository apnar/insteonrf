#!/usr/bin/env python3
"""Compatibility wrapper: same as `insteon-rf print`."""
import sys
from insteonrf.cli import print_main

sys.exit(print_main())
