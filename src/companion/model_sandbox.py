"""Offline macOS model trials; read deny-list, not a complete hostile-code jail.

Python/Metal retain other OS access. Stage models outside protected paths; never
exempt a model from the private-data deny-list. Production inference is unchanged.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DENY_READ = (_ROOT / "data", _ROOT / ".env", *(
    Path.home() / name for name in (
        ".ssh", ".aws", ".codex", ".claude", ".config", "Library/Keychains",
        "Library/Application Support", "Documents", "Desktop", "Downloads",
    )
))


class SandboxUnavailable(RuntimeError):
    """The required sandbox cannot be launched; never retry without it."""


def sandbox_profile(*, model_dir, scratch_dir, deny_read=DEFAULT_DENY_READ, executable=sys.executable):
    """Build an SBPL policy for an absolute interpreter path; resolve symlinks."""
    if not Path(executable).is_absolute():
        raise ValueError("The sandbox requires an absolute interpreter path")
    model, scratch = Path(model_dir).resolve(), Path(scratch_dir).resolve()
    denied = tuple(Path(p).expanduser().resolve() for p in deny_read)
    if model.is_relative_to(scratch) or scratch.is_relative_to(model):
        raise ValueError("Model and writable scratch must be separate trees")
    if any(p.is_relative_to(d) for p in (model, scratch) for d in denied):
        raise ValueError("Stage the model and scratch outside protected paths")
    if any(d.is_relative_to(scratch) for d in denied):
        raise ValueError("Scratch must not contain protected paths")
    def quote(path):
        return json.dumps(str(path), ensure_ascii=False)

    interpreter = Path(executable).resolve()
    executables = [interpreter]
    # macOS framework Python's bin/python is a launcher for this exact binary.
    framework_python = interpreter.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
    if (framework_python.is_file() and interpreter.parent.name == "bin"
            and interpreter.parent.parent.parent.name == "Versions"
            and interpreter.parent.parent.parent.parent.suffix == ".framework"):
        executables.append(framework_python.resolve())
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        "(deny process-fork)", "(deny process-exec*)",
        *(f"(allow process-exec (literal {quote(p)}))" for p in executables),
        "(deny appleevent-send)",
        "(deny lsopen)",
        '(deny mach-lookup (global-name "com.apple.coreservices.appleevents"))',
        '(deny mach-lookup (global-name "com.apple.mDNSResponder"))',
        "(deny file-write*)",
        f"(allow file-write* (subpath {quote(scratch)}))",
        *(f"(deny file-read* (subpath {quote(p)}))" for p in denied),
    ])


def clean_env(*, scratch_dir=None):
    """Construct an allow-list environment, without inheriting credentials."""
    scratch = Path(scratch_dir or tempfile.mkdtemp(prefix="kyra-model-env-")).resolve()
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(scratch),
        "TMPDIR": str(scratch), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    }


def run_sandboxed(argv, *, model_dir, scratch_dir, timeout=600, deny_read=DEFAULT_DENY_READ):
    """Return the child's result, including sandbox failures, with no fallback."""
    if not os.path.isfile(SANDBOX_EXEC) or not os.access(SANDBOX_EXEC, os.X_OK):
        raise SandboxUnavailable("Required macOS sandbox-exec is unavailable")
    profile = sandbox_profile(model_dir=model_dir, scratch_dir=scratch_dir, deny_read=deny_read,
                              executable=argv[0])
    try:
        return subprocess.run(
            [SANDBOX_EXEC, "-p", profile, *argv], cwd=Path(scratch_dir).resolve(),
            env=clean_env(scratch_dir=scratch_dir), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, close_fds=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable("Sandbox launch failed; no fallback attempted") from exc
