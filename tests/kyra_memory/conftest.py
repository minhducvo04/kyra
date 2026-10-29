"""Fixtures for the kyra_memory package tests. Synthetic rows only; every store lives under a temp dir."""
import itertools

import pytest

# Synthetic section headers exercise the caller-supplied header contract.
KYRA_HEADERS = {"rules": "## Rules", "pinned": "## Pinned (from owner)", "hot": "## Often used",
                "facts": "## Facts", "faults": "## Known faults", "recipes": "## Recipe"}
KYRA_SCOPE = "kyra"

_PUBLIC = (1, [])
_CONVERSATION = (2, ["conversation"])
_THIRD_PARTY = (2, ["third_party"])


def fixed_clock():
    """A clock that ticks one second per call, so knowledge_time is the same on every run."""
    ticks = itertools.count()
    return lambda: f"2026-01-01T00:00:{next(ticks) % 60:02d}.000000+00:00"


def fill(store) -> None:
    """The same rows on every call: two rules, a pinned row, facts (one shadowed by the pinned key), faults, a recipe."""
    add = store.add
    add("rules", "Run the linter before every commit.", source="owner", topic="workflow", label=_PUBLIC)
    add("rules", "Never print a secret value.", source="owner", topic="safety", label=_PUBLIC)
    add("pinned", "The staging cache is warmed at six.", source="owner", key="cache-warm", label=_CONVERSATION)
    add("facts", "Staging cache warm time is unknown.", source="agent", key="cache-warm", label=_CONVERSATION)
    add("facts", "The deploy gate needs a green pipeline before the cache warm step.", source="agent",
        topic="deploy", label=_CONVERSATION, confidence=0.8)
    add("facts", "Deploy retries are capped at three.", source="agent", topic="deploy", label=_CONVERSATION)
    add("facts", "The vendor contact prefers email.", source="agent", topic="vendor", label=_THIRD_PARTY)
    add("faults", "pipeline failed: lint error in module a.py:42", source="gate", topic="deploy", label=_CONVERSATION)
    add("recipes", "Open the pipeline page, read the failing job, rerun after the fix.", source="agent",
        topic="deploy gate", label=_CONVERSATION)


@pytest.fixture
def kyra_headers():
    return dict(KYRA_HEADERS)


@pytest.fixture
def scope():
    return KYRA_SCOPE


@pytest.fixture
def root(tmp_path):
    return tmp_path / "mem"


@pytest.fixture
def store(root):
    from kyra_memory.store import MemoryStore

    s = MemoryStore(root, KYRA_SCOPE, create=True, clock=fixed_clock())
    fill(s)
    yield s
    s.close()
