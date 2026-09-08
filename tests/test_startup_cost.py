"""Importing the web app, and building the tool registry, must not load a model.

Both properties are recorded in CLAUDE.md as measurements ("webapp imports in
0.7 s with no model load"; "the registry still builds in 0.55 s and does not
import chromadb") and nothing enforced them. They are easy to break by accident
- one top-level `from companion.search import HybridSearchIndex`, or a store
constructed at module scope, pulls in Chroma and the BGE embedding model - and
the breakage is quiet: the server still works, it just takes tens of seconds to
start and drags the whole test suite with it. That is exactly what _Runtime's
cached_property and LazyBackends exist to prevent.

Asserted as "which modules are in sys.modules", not as a stopwatch: a timing
assertion on a shared CI runner is a flake, while an accidental import is a
fact. Each check runs in its own subprocess, because this test process has
already imported half the world.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# The heavy ones, and what pulls each in: Chroma and BGE come with any memory or
# search store, torch/mlx with a local model, faster_whisper and kokoro with voice.
HEAVY = ("chromadb", "torch", "mlx", "mlx_lm", "sentence_transformers", "faster_whisper", "kokoro")


def _heavy_modules_after(statement: str, tmp_path: Path) -> list[str]:
    env = {
        **os.environ,
        "KYRA_DATA_DIR": str(tmp_path),
        "ANTHROPIC_API_KEY": "test-key-not-real",
        "KYRA_INLINE_WORKER": "false",
        "PYTHONPATH": str(SRC),
    }
    code = f"import sys\n{statement}\nprint(' '.join(m for m in {HEAVY!r} if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.split()


def test_importing_the_web_app_loads_no_model(tmp_path):
    assert _heavy_modules_after("import companion.webapp", tmp_path) == []


def test_building_the_tool_registry_loads_no_model(tmp_path):
    # default_tool_registry() runs at startup in all three front doors, so a
    # store that opens its index eagerly here costs every one of them. This is
    # why SearchKyraDataTool opens the index on first use, not in its __init__.
    statement = (
        "from companion.default_tools import default_tool_registry\n"
        "default_tool_registry()"
    )
    assert _heavy_modules_after(statement, tmp_path) == []


@pytest.mark.parametrize("module", ["companion.digest", "companion.job_boards", "companion.outreach"])
def test_the_scheduled_and_tool_modules_stay_cheap_to_import(module, tmp_path):
    # daily_digest.py runs unattended at 05:00; a heavy import there is a slow
    # start nobody is awake to notice.
    assert _heavy_modules_after(f"import {module}", tmp_path) == []
