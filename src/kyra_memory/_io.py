"""File helpers the package needs; kept here so it imports nothing from the host application."""
import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Write JSON through a sibling temp file and an atomic rename, so a crash leaves the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
