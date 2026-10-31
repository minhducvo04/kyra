"""Test portability markers and marker-to-runner placement (pure, no transport).

Every test is hermetic and portable by default: no marker means it may run on
any runner. A test that needs more declares it with one of ``MARKERS`` (also
registered in ``pyproject.toml``). ``tests/conftest.py`` enforces the set at
collection time. ``place`` maps files' markers to runner ``Capabilities`` so
the runner pool can retire hand-kept skip lists: a file goes only where every
capability it needs is present, or ``Unplaceable`` names what is missing.
Nothing is skipped silently.

Privacy (AGENTS.md rules 1, 2, 6, 10): ``needs_real_data`` requires a local
runner that may see real data, checked here in code, mirroring
``runner_pool.can_run``. ``FILE_MARKERS`` seeds the files whose needs are
already known in tracked code; ``tests/test_test_markers.py`` keeps its
real-data entries equal to ``runner_pool.REAL_DATA_TESTS`` so the two lists
cannot drift while both exist.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

MARKERS: dict[str, str] = {
    "needs_macos": "needs a macOS-only tool or API (sandbox-exec, plutil, launchd)",
    "needs_real_data": "reads the repo's real data/ (owner Mac only)",
    "needs_git_history": "needs more git history than a one-commit archive",
    "needs_network_loopback": "binds or calls 127.0.0.1 / localhost",
    "needs_gpu": "needs GPU VRAM",
    "timing_sensitive": "asserts on wall-clock timing; place on a low-load runner",
}

# Prefixes a capability marker may start with; any other marker starting with
# one of these is a typo the conftest check refuses.
MARKER_PREFIXES = ("needs_", "timing_")

# Files whose needs are already known, so collection can tag them before the
# per-file audit (PLAN.md section 1) adds markers at the source. The
# needs_real_data rows must mirror runner_pool.REAL_DATA_TESTS exactly.
FILE_MARKERS: dict[str, tuple[str, ...]] = {
    "tests/test_no_target_companies.py": ("needs_real_data",),
    "tests/test_no_third_party_pii.py": ("needs_real_data",),
    "tests/test_no_personal_files.py": ("needs_real_data",),
    "tests/test_home_router_holdout2.py": ("needs_real_data",),
    "tests/test_home_router_holdout3.py": ("needs_real_data",),
    "tests/test_router_train_real.py": ("needs_real_data",),
    "tests/test_fleet_eval.py": ("needs_git_history",),
    "tests/test_deploy_local_builder.py": ("needs_network_loopback",),
    "tests/test_intern_local.py": ("timing_sensitive",),
    "tests/test_intern_runner.py": ("timing_sensitive",),
}


class Unplaceable(ValueError):
    """A file has no runner with every capability it needs; never skip it."""


@dataclass(frozen=True)
class Capabilities:
    """What one runner can offer, as reported by its probe (PLAN.md section 3)."""

    os: str = ""  # "macos", "linux", "linux-wsl2", ...
    reach: str = "ssh"  # "local" or "ssh", mirroring runner_pool.Runner
    may_see_real_data: bool = False
    git_history: bool = False  # full clone, not a one-commit archive
    loopback: bool = False  # probe bound and fetched 127.0.0.1 successfully
    gpu_vram_gb: float = 0
    timing_safe: bool = False  # low load: wall-clock asserts are meaningful


def validate_markers(markers: Iterable[str]) -> tuple[str, ...]:
    """Return the markers sorted, or raise ValueError naming the unknown ones."""
    found = tuple(sorted(set(markers)))
    unknown = [m for m in found if m.startswith(MARKER_PREFIXES) and m not in MARKERS]
    if unknown:
        raise ValueError(f"unknown portability markers: {', '.join(unknown)}; known: {', '.join(sorted(MARKERS))}")
    return tuple(m for m in found if m in MARKERS)


def missing_capabilities(markers: Iterable[str], caps: Capabilities) -> tuple[str, ...]:
    """The declared markers this runner cannot satisfy (empty means it can run)."""
    missing = []
    for marker in validate_markers(markers):
        if marker == "needs_macos" and caps.os != "macos":
            missing.append(marker)
        elif marker == "needs_real_data" and not (caps.may_see_real_data and caps.reach == "local"):
            missing.append(marker)
        elif marker == "needs_git_history" and not caps.git_history:
            missing.append(marker)
        elif marker == "needs_network_loopback" and not caps.loopback:
            missing.append(marker)
        elif marker == "needs_gpu" and not caps.gpu_vram_gb > 0:
            missing.append(marker)
        elif marker == "timing_sensitive" and not caps.timing_safe:
            missing.append(marker)
    return tuple(missing)


def eligible(markers: Iterable[str], runners: Mapping[str, Capabilities]) -> tuple[str, ...]:
    """Names of runners that satisfy every marker, sorted for determinism."""
    return tuple(name for name in sorted(runners) if not missing_capabilities(markers, runners[name]))


def place(files: Mapping[str, Iterable[str]], runners: Mapping[str, Capabilities]) -> dict[str, list[str]]:
    """Assign each file to the least-loaded eligible runner (file count, for now).

    ``files`` maps a test file path to its markers. Duration-weighted
    balancing stays in ``runner_pool.shard``; this is the capability gate it
    will consult. Raises ``Unplaceable`` listing every file no runner can
    take, with the markers each one needs. Every file lands exactly once.
    """
    plan: dict[str, list[str]] = {}
    counts = dict.fromkeys(runners, 0)
    stuck: list[str] = []
    for path in sorted(files):
        markers = validate_markers(files[path])
        choices = eligible(markers, runners)
        if not choices:
            stuck.append(f"{path} needs {', '.join(markers) or 'no markers'}")
            continue
        best = min(choices, key=lambda name: (counts[name], name))
        plan.setdefault(best, []).append(path)
        counts[best] += 1
    if stuck:
        raise Unplaceable("no runner can take: " + "; ".join(stuck))
    placed = [p for paths in plan.values() for p in paths]
    if sorted(placed) != sorted(files):  # post-condition: each file exactly once
        raise AssertionError("place lost or duplicated a file")
    return plan


def capabilities_from_env(value: str | None) -> Capabilities | None:
    """Parse ``KYRA_RUNNER_CAPABILITIES`` for the conftest skip check.

    Format: comma-separated tokens. The first token is the OS name; the rest
    name what the runner offers: ``local``, ``real_data``, ``git_history``,
    ``loopback``, ``gpu``, ``timing_safe``. Example for the PC:
    ``linux-wsl2,git_history,loopback,gpu``. ``None`` (unset) means no
    capability gate, as on a developer machine.
    """
    if value is None or not value.strip():
        return None
    tokens = [t.strip() for t in value.split(",") if t.strip()]
    known = {"local", "real_data", "git_history", "loopback", "gpu", "timing_safe"}
    unknown = [t for t in tokens[1:] if t not in known]
    if unknown:
        raise ValueError(f"unknown KYRA_RUNNER_CAPABILITIES tokens: {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    offers = set(tokens[1:])
    return Capabilities(
        os=tokens[0],
        reach="local" if "local" in offers else "ssh",
        may_see_real_data="real_data" in offers,
        git_history="git_history" in offers,
        loopback="loopback" in offers,
        gpu_vram_gb=1.0 if "gpu" in offers else 0,
        timing_safe="timing_safe" in offers,
    )


def seed_markers_for(path: str, declared: Sequence[str] = ()) -> tuple[str, ...]:
    """Markers for a file: what it declares plus its seed-map entry, validated."""
    return validate_markers([*declared, *FILE_MARKERS.get(path, ())])
