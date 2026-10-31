"""Machine capability probe: facts about this machine, nothing identifying it.

``probe`` reports OS and architecture, cores, RAM, tools found on PATH,
importable Python packages from a requirements file, whether a loopback
bind and fetch on 127.0.0.1 works, the git history depth of a repo, GPUs
and their VRAM, and whether the repo's ``data/`` directory is present and
readable (``may_see_real_data``). The result is a plain dict whose
``capabilities`` entry is exactly the fields of
``companion.test_markers.Capabilities``; ``to_capabilities`` converts.

Universal by construction (PLAN.md slice TS1): every external call is
injected (``run``, ``env``, ``which``, ``loopback_check``, ``find_spec``,
``system``, ``machine``, ``cpu_count``, ``total_ram_bytes``), so tests feed
canned output for any OS and no code path assumes a particular machine,
operating system tool, or toolchain. OS-specific commands run only when
the probed OS matches, and a missing tool is a fact (``None`` from
``run``), never an exception. Nothing that names or locates the machine
is read or returned: facts only.

``timing_safe`` is always ``False`` here: it depends on live load, which
``runner_probe`` owns, not on the machine's static capabilities.
``reach`` is ``"local"`` because the probe runs on the machine it
describes. Standard library only.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from companion.test_markers import Capabilities

# Tools looked for on PATH unless the caller passes its own list. A tool
# absent from this list is simply not probed; absence from PATH is a fact.
DEFAULT_TOOLS: tuple[str, ...] = ("python3", "git", "node", "npm", "pytest", "ruff", "ssh")

# Distribution name -> import name where the plain rule (lowercase,
# ``-`` and ``.`` to ``_``) is wrong.
IMPORT_NAMES: dict[str, str] = {
    "beautifulsoup4": "bs4",
    "cadquery-ocp-novtk": "OCP",
    "google-api-python-client": "googleapiclient",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "pillow": "PIL",
    "pyjwt": "jwt",
    "python-docx": "docx",
    "python-dotenv": "dotenv",
    "python-multipart": "multipart",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
}

CAPABILITY_KEYS: tuple[str, ...] = (
    "os", "reach", "may_see_real_data", "git_history", "loopback", "gpu_vram_gb", "timing_safe",
)

_DIST_SEP = re.compile(r"[\s\[;<>=!~(.]")
_MEMTOTAL = re.compile(r"^MemTotal:\s*(\d+)\s*kB", re.M)


def run_command(argv: Sequence[str]) -> str | None:
    """Production ``run``: stdout on exit 0, ``None`` on any failure."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def check_loopback(timeout: float = 2.0) -> bool:
    """Production loopback check: bind 127.0.0.1, connect, fetch one byte."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.settimeout(timeout)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=timeout) as client:
                conn, _addr = server.accept()
                with conn:
                    conn.sendall(b"k")
                    return client.recv(1) == b"k"
    except OSError:
        return False


def _sysconf_ram_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None


def parse_requirements(text: str) -> list[str]:
    """Distribution names from requirements text, in file order, deduplicated."""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = _DIST_SEP.split(line, 1)[0].strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def import_name(dist: str) -> str:
    return IMPORT_NAMES.get(dist, dist.replace("-", "_").replace(".", "_"))


def _norm_os(system: str, env: Mapping[str, str], run: Callable[[list[str]], str | None]) -> str:
    name = system.strip().lower()
    if name == "darwin":
        return "macos"
    if name == "linux":
        if "WSL_DISTRO_NAME" in env or "WSL_INTEROP" in env:
            return "linux-wsl2"
        version = (run(["cat", "/proc/version"]) or "").lower()
        if "microsoft" in version or "wsl" in version:
            return "linux-wsl2"
        return "linux"
    return name or "unknown"


def _norm_arch(machine: str) -> str:
    arch = machine.strip().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(arch, arch or "unknown")


def _int_from(text: str | None) -> int | None:
    match = re.search(r"\d+", text or "")
    return int(match[0]) if match else None


def _cores(os_name: str, env: Mapping[str, str], run, cpu_count) -> int:
    found: int | None = None
    if os_name == "macos":
        found = _int_from(run(["sysctl", "-n", "hw.ncpu"]))
    elif os_name.startswith("linux"):
        found = _int_from(run(["nproc"]))
    elif os_name == "windows":
        found = _int_from(env.get("NUMBER_OF_PROCESSORS"))
    if found is None:
        try:
            found = cpu_count()
        except Exception:  # noqa: BLE001 - a failing fallback is "unknown", not a crash
            found = None
    return int(found) if found else 0


def _ram_bytes(os_name: str, run, total_ram_bytes) -> int | None:
    found: int | None = None
    if os_name == "macos":
        found = _int_from(run(["sysctl", "-n", "hw.memsize"]))
    elif os_name.startswith("linux"):
        match = _MEMTOTAL.search(run(["cat", "/proc/meminfo"]) or "")
        found = int(match[1]) * 1024 if match else None
    elif os_name == "windows":
        found = _int_from(run(["wmic", "computersystem", "get", "TotalPhysicalMemory"]))
    if found is None:
        try:
            found = total_ram_bytes()
        except Exception:  # noqa: BLE001
            found = None
    return found


def _gpus(os_name: str, run) -> list[dict[str, float | str]]:
    gpus: list[dict[str, float | str]] = []
    out = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    for line in (out or "").splitlines():
        name, comma, mib = line.partition(",")
        value = _int_from(mib) if comma else None
        if name.strip() and value is not None:
            gpus.append({"name": name.strip(), "vram_gb": round(value / 1024, 3)})
    if gpus or os_name != "macos":
        return gpus
    out = run(["system_profiler", "SPDisplaysDataType"]) or ""
    names = re.findall(r"Chipset Model:\s*(.+)", out)
    vrams = re.findall(r"VRAM \(Total\):\s*([0-9.]+)\s*([GM]B)", out)
    for index, name in enumerate(names):
        vram_gb = 0.0
        if index < len(vrams):
            value, unit = float(vrams[index][0]), vrams[index][1]
            vram_gb = value if unit == "GB" else round(value / 1024, 3)
        gpus.append({"name": name.strip(), "vram_gb": vram_gb})
    return gpus


def _git_depth(repo: Path, run) -> int:
    return _int_from(run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"])) or 0


def _packages(repo: Path, requirements: str | Path | None, find_spec) -> dict[str, bool]:
    path = Path(requirements) if requirements is not None else repo / "requirements.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    result: dict[str, bool] = {}
    for dist in parse_requirements(text):
        try:
            result[dist] = find_spec(import_name(dist)) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            result[dist] = False
    return result


def probe(
    repo: str | Path = ".",
    *,
    run: Callable[[list[str]], str | None] | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    loopback_check: Callable[[], bool] | None = None,
    find_spec: Callable[[str], object | None] | None = None,
    system: str | None = None,
    machine: str | None = None,
    cpu_count: Callable[[], int | None] | None = None,
    total_ram_bytes: Callable[[], int | None] | None = None,
    tools: Sequence[str] = DEFAULT_TOOLS,
    requirements: str | Path | None = None,
) -> dict:
    """Probe this machine against ``repo``; return facts plus ``capabilities``.

    Every external call goes through an injected callable; defaults are the
    production ones. ``run(argv)`` returns stdout or ``None``.
    """
    run = run_command if run is None else run
    env = os.environ if env is None else env
    which = (lambda name: shutil.which(name, path=env.get("PATH"))) if which is None else which
    loopback_check = check_loopback if loopback_check is None else loopback_check
    find_spec = importlib.util.find_spec if find_spec is None else find_spec
    cpu_count = os.cpu_count if cpu_count is None else cpu_count
    total_ram_bytes = _sysconf_ram_bytes if total_ram_bytes is None else total_ram_bytes

    repo = Path(repo)
    os_name = _norm_os(platform.system() if system is None else system, env, run)
    ram = _ram_bytes(os_name, run, total_ram_bytes)
    gpus = _gpus(os_name, run)
    depth = _git_depth(repo, run)
    data_dir = repo / "data"
    try:
        loopback = bool(loopback_check())
    except Exception:  # noqa: BLE001 - a failing check is the fact "no loopback"
        loopback = False

    capabilities = {
        "os": os_name,
        "reach": "local",
        "may_see_real_data": data_dir.is_dir() and os.access(data_dir, os.R_OK | os.X_OK),
        "git_history": depth > 1,
        "loopback": loopback,
        "gpu_vram_gb": max((float(gpu["vram_gb"]) for gpu in gpus), default=0.0),
        "timing_safe": False,
    }
    return {
        **capabilities,
        "arch": _norm_arch(platform.machine() if machine is None else machine),
        "cores": _cores(os_name, env, run, cpu_count),
        "ram_gb": round(ram / 1024**3, 3) if ram else 0.0,
        "tools": sorted(name for name in tools if which(name)),
        "packages": _packages(repo, requirements, find_spec),
        "git_depth": depth,
        "gpus": gpus,
        "capabilities": capabilities,
    }


def to_capabilities(facts: Mapping) -> Capabilities:
    """``Capabilities`` from a probe result (or its ``capabilities`` entry)."""
    caps = facts.get("capabilities")
    source = caps if isinstance(caps, Mapping) else facts
    return Capabilities(**{key: source[key] for key in CAPABILITY_KEYS if key in source})
