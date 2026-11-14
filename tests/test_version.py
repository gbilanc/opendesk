"""Il __version__ del package deve coincidere con pyproject.toml.

RCA 2026-11-14: __version__ hard-coded restava a 2.1.0 dopo il bump del
pyproject a 2.2.1 — installer etichettati male e debugging confuso.
"""

import tomllib
from pathlib import Path

from opendesk import __version__


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert __version__ == data["project"]["version"]
