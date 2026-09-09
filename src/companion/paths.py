"""One place every on-disk location is derived from.

Before this existed, every module computed
`Path(__file__).resolve().parent.parent.parent / "data" / ...` on its own
(eleven copies), and memory.py used a plain relative "data/memory_db" -
which silently created a brand-new, empty memory store whenever the
process was started from any directory other than the project root.
Centralizing the root fixes that class of bug and, more importantly,
makes the whole data directory overridable with a single environment
variable (`KYRA_DATA_DIR`) - the 12-factor "config in the environment"
convention - so a test run can point every store at a scratch directory
without touching Duc's real reminders/learning/job data, and a future
deployment can mount a volume anywhere.

Modules keep their own `DEFAULT_*` constants (public names other code
already imports) but derive them from DATA_DIR here.
"""
import json
import os
from pathlib import Path
from typing import Any

from companion.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(get_settings().data_dir).resolve()
WEB_DIR = PROJECT_ROOT / "web"

# Compiled one-page resume PDFs served by /api/job/resume-pdf/{filename}.
GENERATED_RESUMES_DIR = DATA_DIR / "generated_resumes"

# Duc's resumes: the hand-kept .tex/.pdf pairs and the ones apply_pipeline.py tailors per posting.
RESUMES_DIR = DATA_DIR / "resumes"


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Write JSON so an interrupted write cannot destroy what was there.

    `Path.write_text` truncates the file before it writes, so a crash in
    between leaves it empty or half-written - and the files this is used for
    are curated: the document library index (Duc's resumes and style samples,
    read by every drafting path), the board watchlist he maintains by hand,
    and the seen-postings set. Every reader here already fails loud on a
    corrupt file, which is right; the problem this solves is that the contents
    are then gone, not that a bad file is read quietly.

    Writing a sibling temp file and renaming makes that impossible: `os.replace`
    is atomic on POSIX, so a reader sees either the whole old file or the whole
    new one. The temp file is a sibling, not in /tmp, because rename is only
    atomic within one filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
