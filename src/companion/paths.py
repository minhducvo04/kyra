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
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("KYRA_DATA_DIR") or (PROJECT_ROOT / "data")).resolve()
WEB_DIR = PROJECT_ROOT / "web"
