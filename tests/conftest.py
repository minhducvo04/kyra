"""Test-wide isolation. Every store in companion/ derives its on-disk
location from companion.paths.DATA_DIR, which reads KYRA_DATA_DIR at
import time - so pointing that at a scratch directory BEFORE any
companion import means a test run can never touch Duc's real
reminders.db / learning.db / job data / router.log. (Before this
existed, classifier re-test scripts really did leave 193 lines of test
noise in the real router.log - see CLAUDE.md.)
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="kyra_test_data_"))
os.environ["KYRA_DATA_DIR"] = str(_SCRATCH)
# A syntactically-present key so modules that build an Anthropic client
# at import (webapp.py, default_tools.py) can be imported. No test may
# make a real API call - every LLM in tests is a ScriptedLLM (fakes.py).
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
# Settings also reads the developer's real .env; pin the knobs a test must not inherit
# (a configured fine-tuned classifier would silently change which router path tests exercise).
os.environ["KYRA_CLASSIFIER_ADAPTER"] = ""
# Tests drive the job queue explicitly with run_one(); no background thread racing them.
os.environ["KYRA_INLINE_WORKER"] = "false"
# Starting the app in a test must never load the classifier (~44s, and it would
# race every test that starts a TestClient).
os.environ["KYRA_WARM_UP_ROUTER"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup_scratch():
    yield
    shutil.rmtree(_SCRATCH, ignore_errors=True)


@pytest.fixture
def scratch_dir() -> Path:
    return _SCRATCH
