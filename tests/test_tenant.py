"""Father's system is a separate tenant, enforced at startup (red until Codex builds it).

Plan: docs/plans/2026-09-16-father.md. The handoff's non-negotiables: separate data, no training on his
content, direct approved providers only, no router. A prompt cannot promise these; a refusal at startup can.

CONTRACT
  Settings.tenant: str = "personal"          # KYRA_TENANT, one of {"personal", "father"}
  Settings() raises ValueError for tenant "father" when KYRA_DATA_DIR is unset, or when it resolves inside
      PROJECT_ROOT / "data" (the personal directory). Message contains "tenant".
  companion.tenant:
    @dataclass(frozen=True) TenantPolicy: name, min_tier, training_allowed, local_models_allowed,
        router_allowed, providers: frozenset[str]
    policy(settings) -> TenantPolicy
        personal: min_tier "casual", training True, local True, router True, providers {"anthropic", "openai", "local"}
        father:   min_tier "work",   training False, local False, router False, providers {"anthropic", "openai"}
    class TenantRefused(RuntimeError)
    refuse_training(settings) -> None      # raises TenantRefused for father
  scripts/router_ft.py and scripts/tool_ft.py call refuse_training before doing anything else.
"""
from pathlib import Path

import pytest

from companion.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def _settings(monkeypatch, *, tenant=None, data_dir=None):
    for key in ("KYRA_TENANT", "KYRA_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    if tenant is not None:
        monkeypatch.setenv("KYRA_TENANT", tenant)
    if data_dir is not None:
        monkeypatch.setenv("KYRA_DATA_DIR", str(data_dir))
    return Settings(_env_file=None)


def test_personal_is_the_default_and_unchanged(monkeypatch, tmp_path):
    s = _settings(monkeypatch, data_dir=tmp_path)
    assert s.tenant == "personal" and s.data_dir == tmp_path


def test_unknown_tenant_is_refused(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        _settings(monkeypatch, tenant="uncle", data_dir=tmp_path)


def test_father_refuses_the_personal_data_directory(monkeypatch):
    with pytest.raises(ValueError, match="tenant"):
        _settings(monkeypatch, tenant="father")  # unset -> the personal default
    with pytest.raises(ValueError, match="tenant"):
        _settings(monkeypatch, tenant="father", data_dir=ROOT / "data" / "father")  # nested inside it


def test_father_starts_with_its_own_directory(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tenant="father", data_dir=tmp_path / "father-data")
    assert s.tenant == "father"


def test_policy_per_tenant(monkeypatch, tmp_path):
    from companion.tenant import policy

    personal = policy(_settings(monkeypatch, data_dir=tmp_path))
    father = policy(_settings(monkeypatch, tenant="father", data_dir=tmp_path / "f"))
    assert (personal.min_tier, personal.training_allowed, personal.router_allowed) == ("casual", True, True)
    assert (father.min_tier, father.training_allowed, father.local_models_allowed, father.router_allowed) == ("work", False, False, False)
    assert father.providers == frozenset({"anthropic", "openai"}) and "local" in personal.providers


def test_training_is_refused_for_father_and_the_scripts_ask_first(monkeypatch, tmp_path):
    from companion.tenant import TenantRefused, refuse_training

    refuse_training(_settings(monkeypatch, data_dir=tmp_path))  # personal: silent
    with pytest.raises(TenantRefused):
        refuse_training(_settings(monkeypatch, tenant="father", data_dir=tmp_path / "f"))
    for script in ("router_ft.py", "tool_ft.py"):
        assert "refuse_training" in (ROOT / "scripts" / script).read_text(encoding="utf-8"), script
