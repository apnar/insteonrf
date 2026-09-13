#!/usr/bin/env python3
"""Compatibility wrapper: same as `insteon-rf send`."""
import sys
from insteonrf.cli import send_main

sys.exit(send_main())
