"""Shared utility helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write private JSON configuration atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp:
            temp_name = temp.name
            os.chmod(temp.name, 0o600)
            json.dump(data, temp, indent=2)
            temp.flush()
            os.fsync(temp.fileno())
        Path(temp_name).replace(path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
