"""Open-weight models on trial run where they can reach nothing (the owner condition, 2026-09-20).

"If make sure no possible way that it leaked my data or train or do something on my machine then okay." Nobody can promise
"no possible way" for software; what CAN be built and proven is: no network path, no credentials, no access to the owner data,
writes only into a scratch folder. These tests prove it with the real macOS sandbox, for every model of every origin.

CONTRACT (companion/model_sandbox.py), plain functions:
  sandbox_profile(*, model_dir, scratch_dir, deny_read) -> str     an SBPL profile: deny network*, deny file-write* except
      scratch_dir, deny file-read* under every path in deny_read, allow the rest (a Python + Metal process cannot start
      under a read allow-list; that limit is stated, not hidden)
  DEFAULT_DENY_READ: the repo's data/ directory and .env, and under the home folder .ssh, .aws, .codex, .claude, .config,
      Library/Keychains, Library/Application Support, Documents, Desktop, Downloads
  clean_env() -> dict   PATH, HOME=<scratch>, TMPDIR=<scratch>, HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 and nothing else:
      no *KEY*, *TOKEN*, *SECRET*, AWS_*, ANTHROPIC_*, OPENAI_*, KYRA_* variable survives
  run_sandboxed(argv, *, model_dir, scratch_dir, timeout=600, deny_read=DEFAULT_DENY_READ) -> subprocess.CompletedProcess
      runs argv under /usr/bin/sandbox-exec with that profile and clean_env(); raises SandboxUnavailable where the tool
      does not exist (never falls back to running unsandboxed)
"""
import os
import sys

import pytest

from companion import model_sandbox as ms

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                                reason="the macOS sandbox is the mechanism under test")


@pytest.fixture
def dirs(tmp_path):
    model, scratch, private = tmp_path / "model", tmp_path / "scratch", tmp_path / "private"
    for d in (model, scratch, private):
        d.mkdir()
    (model / "weights.safetensors").write_text("numbers only")
    (private / "health.txt").write_text("must never be readable")
    return model, scratch, private


def _py(code, dirs, **kw):
    model, scratch, private = dirs
    return ms.run_sandboxed([sys.executable, "-c", code], model_dir=model, scratch_dir=scratch,
                            deny_read=[private], timeout=60, **kw)


def test_the_process_cannot_open_any_network_connection(dirs):
    code = ("import socket,sys\n"
            "for target in (('1.1.1.1', 53), ('127.0.0.1', 8420)):\n"
            "    try:\n        socket.create_connection(target, 2); print('CONNECTED', target); sys.exit(1)\n"
            "    except OSError as e:\n        print('blocked', target, type(e).__name__)\n"
            "try:\n    socket.getaddrinfo('huggingface.co', 443); print('DNS WORKED'); sys.exit(1)\n"
            "except OSError:\n    print('dns blocked')\n")
    done = _py(code, dirs)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "CONNECTED" not in done.stdout and "DNS WORKED" not in done.stdout


def test_it_reads_the_model_but_not_the_private_folder(dirs):
    model, _, private = dirs
    code = (f"print(open({str(model / 'weights.safetensors')!r}).read())\n"
            f"try:\n    open({str(private / 'health.txt')!r}).read(); print('LEAK')\n"
            "except OSError as e:\n    print('denied', type(e).__name__)\n")
    done = _py(code, dirs)
    assert "numbers only" in done.stdout and "LEAK" not in done.stdout and "denied" in done.stdout


def test_it_writes_only_into_scratch(dirs):
    model, scratch, _ = dirs
    code = (f"open({str(scratch / 'out.json')!r}, 'w').write('ok')\n"
            f"try:\n    open({str(model / 'tamper.bin')!r}, 'w').write('x'); print('WROTE MODEL DIR')\n"
            "except OSError:\n    print('model dir is read-only')\n")
    done = _py(code, dirs)
    assert (scratch / "out.json").read_text() == "ok"
    assert "WROTE MODEL DIR" not in done.stdout and not (model / "tamper.bin").exists()


def test_no_secret_reaches_the_process(dirs, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "KYRA_API_TOKEN", "HF_TOKEN", "MY_SECRET"):
        monkeypatch.setenv(name, "planted-value")
    done = _py("import os; print(sorted(os.environ))", dirs)
    assert "planted-value" not in done.stdout
    for name in ("ANTHROPIC", "OPENAI", "AWS_", "KYRA_", "TOKEN", "SECRET", "KEY"):
        assert name not in done.stdout, f"{name} survived into the sandbox: {done.stdout}"
    assert "HF_HUB_OFFLINE" in done.stdout


def test_the_default_deny_list_covers_the_owners_data():
    listed = " ".join(str(p) for p in ms.DEFAULT_DENY_READ)
    for needle in ("/data", ".env", ".ssh", ".aws", ".codex", ".claude", "Keychains", "Documents", "Desktop", "Downloads"):
        assert needle in listed, needle


def test_it_never_falls_back_to_running_without_the_sandbox(dirs, monkeypatch):
    monkeypatch.setattr(ms, "SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    with pytest.raises(ms.SandboxUnavailable):
        _py("print('ran without a sandbox')", dirs)


def test_the_process_can_launch_no_other_program(dirs):
    """Claude's adversarial check, 2026-09-21: everything else was blocked, but /usr/bin/osascript still ran, and Apple Events
    can ask an UNSANDBOXED app to do the network or file work. Inference needs no child process at all."""
    code = ("import subprocess\n"
            "for argv in (['/usr/bin/osascript', '-e', 'return 1'], ['/bin/sh', '-c', 'echo hi'], ['/usr/bin/open', '-g', 'https://example.com']):\n"
            "    try:\n        r = subprocess.run(argv, capture_output=True, timeout=10); print('LAUNCHED', argv[0], r.returncode)\n"
            "    except OSError as e:\n        print('blocked', argv[0], type(e).__name__)\n")
    done = _py(code, dirs)
    assert "LAUNCHED" not in done.stdout, done.stdout
    assert done.stdout.count("blocked") == 3, done.stdout + done.stderr
