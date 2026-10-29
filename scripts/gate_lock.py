"""Hold or wait for the landing-gate lock (see companion.gate_lock).

    python scripts/gate_lock.py run -- .venv/bin/python scripts/workflow_land.py <target>   # a direct landing
    python scripts/gate_lock.py wait && python -m pytest ...                                  # builders, reviewers
    python scripts/gate_lock.py status

Exit codes: run returns the command's code (4 when another gate run holds the lock); wait 0 when free, 3 on timeout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.gate_lock import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
