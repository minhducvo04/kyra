"""The kyra-memory quickstart example runs as a copyable script in a temp directory."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "packages" / "kyra-memory" / "examples" / "quickstart.py"


def test_quickstart_runs_as_a_subprocess_in_tmp(tmp_path):
    script = tmp_path / "quickstart.py"
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
        "acme added id=1",
        "globex added id=1",
        "acme search spare key: 1",
        "globex search spare key: 0",
        "globex search weekly visit: 1",
        "acme search weekly visit: 0",
        "acme forget id=1 rows=1",
        "acme search spare key: 0",
        "globex search weekly visit: 1",
    ]
