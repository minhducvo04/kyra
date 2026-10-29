import json
import os
from pathlib import Path
from typing import Any
from companion.settings import get_settings
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(get_settings().data_dir).resolve()
WEB_DIR = PROJECT_ROOT / 'web'

def write_json(path: Path, data: Any, *, indent: int=2) -> None:
    """Write JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(data, indent=indent), encoding='utf-8')
    os.replace(tmp, path)
