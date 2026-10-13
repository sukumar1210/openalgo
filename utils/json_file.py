"""
A JSON file written whole or not at all.

The file is written to a temporary name of its own beside it, flushed to disk,
and renamed over the original, so a crash mid-write leaves the previous
version, never half of each. Callers serialise their own writers; the unique
temporary name only keeps two writers from sharing one.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def read_json(path: Path, default):
    """The file's content, or ``default`` when it does not exist yet."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json_atomic(path: Path, data) -> None:
    """Replace ``path`` with ``data`` as JSON, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
