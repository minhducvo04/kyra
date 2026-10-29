"""The kyra-memory search_many example runs as a copyable script in a temp directory."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "packages" / "kyra-memory" / "examples" / "search_many.py"


def test_search_many_runs_as_a_subprocess_in_tmp(tmp_path):
    script = tmp_path / "search_many.py"
    shutil.copy(EXAMPLE, script)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].startswith("store: ")
    assert lines[1:] == [
        "loaded: 50",
        "search spare key: 10 boiler needs spare key",
        "search weekly visit: 10 boiler needs weekly visit",
        "search boiler: 5 boiler needs spare key",
        "search invoice: 5 invoice needs spare key",
        "search missing widget: 0",
    ]
