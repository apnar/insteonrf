"""Shared fixtures.

Hardware tests are skipped unless ``INSTEONRF_HW=1``. Tests that use
insteon-mqtt as a decoding oracle are skipped unless ``INSTEONRF_IMQTT``
points at its source tree.
"""

import importlib
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: needs a real radio (set INSTEONRF_HW=1)")
    config.addinivalue_line(
        "markers", "imqtt: needs insteon-mqtt as an oracle (set INSTEONRF_IMQTT)"
    )


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


@pytest.fixture(scope="session")
def imqtt_message():
    """insteon-mqtt's own ``message`` package, used as a decoding oracle.

    Deliberately not vendored — it is GPL and this repo should not carry a
    copy. Point ``INSTEONRF_IMQTT`` at a checkout, or at a copy taken out of
    the running container::

        kubectl exec homeassistant -c insteon -- tar cf - -C /opt/insteon-mqtt \
            insteon_mqtt | tar xf - -C /tmp/imqtt
        INSTEONRF_IMQTT=/tmp/imqtt pytest -m imqtt

    ``insteon_mqtt/__init__.py`` imports jinja2 and the whole MQTT layer, so a
    plain import would drag in dependencies these tests do not need. A stub
    parent package with ``__path__`` set lets the pure protocol submodules
    resolve their relative imports without running that ``__init__``.
    """
    root = os.environ.get("INSTEONRF_IMQTT")
    if not root:
        pytest.skip("set INSTEONRF_IMQTT to an insteon-mqtt source tree")
    pkg_dir = pathlib.Path(root) / "insteon_mqtt"
    if not pkg_dir.is_dir():
        pytest.skip(f"no insteon_mqtt package under {root}")
    if "insteon_mqtt" not in sys.modules:
        stub = types.ModuleType("insteon_mqtt")
        stub.__path__ = [str(pkg_dir)]
        sys.modules["insteon_mqtt"] = stub
    return importlib.import_module("insteon_mqtt.message")
