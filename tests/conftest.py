"""Shared fixtures. Hardware tests are skipped unless INSTEONRF_HW=1."""

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: needs a real radio (set INSTEONRF_HW=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("INSTEONRF_HW") == "1":
        return
    skip = pytest.mark.skip(reason="needs hardware; set INSTEONRF_HW=1 to run")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def fsk2_demod():
    """Path to the compiled C demodulator, building it once if a compiler exists."""
    binary = ROOT / "fsk2_demod"
    if not binary.exists() and shutil.which("make") and shutil.which("cc"):
        subprocess.run(["make", "fsk2_demod"], cwd=ROOT, capture_output=True, check=False)
    if not binary.exists():
        pytest.skip("fsk2_demod is not built (run 'make')")
    return binary
