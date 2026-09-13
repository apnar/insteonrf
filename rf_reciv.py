#!/usr/bin/env python3
"""Compatibility wrapper: same as `insteon-rf recv`."""
import sys

from insteonrf.cli import recv_main

sys.exit(recv_main())
