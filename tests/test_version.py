"""The version number is written once.

It used to be written twice, in ``pyproject.toml`` and in ``__init__.py``,
and by 2.7.2 the two had drifted three releases apart: every pod reported
2.5.4 while running 2.7.2 code, which makes "is this deployment current?"
unanswerable from the outside. These tests keep the second copy from coming
back.
"""

from __future__ import annotations

import pathlib
import re

import tomllib

import insteonrf
from insteonrf import _version

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_the_package_exposes_the_version():
    assert insteonrf.__version__ is _version.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", insteonrf.__version__)


def test_pyproject_does_not_carry_a_second_copy():
    data = _project()
    assert "version" not in data["project"], (
        "a literal version here is the drift this module exists to prevent"
    )
    assert "version" in data["project"]["dynamic"]


def test_pyproject_points_at_the_one_source():
    attr = _project()["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "insteonrf._version.__version__"


def test_the_version_module_imports_nothing():
    """setuptools reads it statically, but only while it stays parseable
    without executing; an import here would make the build import the whole
    package -- numpy, rflib and all -- to learn its own version number."""
    src = pathlib.Path(_version.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s", src, re.MULTILINE)


def test_the_listener_firmware_declares_the_same_version():
    """The Heltec's firmware carries the repo version too.

    ESPHome's own build_time_str only moves when the config hash changes, so
    a reflash of unchanged config keeps the original timestamp -- after an
    OTA on 2026-09-23 the board was still announcing a build time from four
    days earlier. The project version is the marker that answers "is this
    board running current code", so it has to track the package.
    """
    yaml = (PYPROJECT.parent / "esphome" / "insteon-rf-main.yaml").read_text(encoding="utf-8")
    m = re.search(r"^\s*project:\s*$\s*^\s*name:\s*\S+\s*$\s*^\s*version:\s*\"([^\"]+)\"",
                  yaml, re.MULTILINE)
    assert m, "the listener firmware should declare esphome.project.version"
    assert m.group(1) == insteonrf.__version__, (
        "bump insteonrf/_version.py and the firmware's project version together"
    )
