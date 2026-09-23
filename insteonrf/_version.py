"""The one place the version number is written.

Deliberately a module of its own, with no imports. ``pyproject.toml`` reads
it with setuptools' ``dynamic``/``attr``, which parses the file statically
rather than importing it -- so the build never has to import the package
(and therefore numpy, rflib and the rest) just to learn its own version.

It used to be written twice, in ``pyproject.toml`` and in ``__init__.py``,
and by 2.7.2 the two had drifted three releases apart: every pod reported
2.5.4 while running 2.7.2 code, which makes "is this deployment current?"
unanswerable from the outside.
"""

__version__ = "2.8.0"
