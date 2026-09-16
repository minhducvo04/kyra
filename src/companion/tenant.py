"""Small, immutable tenant policies shared by entry points."""
from dataclasses import dataclass

from companion.settings import Settings


@dataclass(frozen=True)
class TenantPolicy:
    name: str
    min_tier: str
    training_allowed: bool
    local_models_allowed: bool
    router_allowed: bool
    providers: frozenset[str]


class TenantRefused(RuntimeError):
    """An operation is forbidden by the configured tenant policy."""


def policy(settings: Settings) -> TenantPolicy:
    if settings.tenant == "personal":
        return TenantPolicy("personal", "casual", True, True, True, frozenset({"anthropic", "openai", "local"}))
    if settings.tenant == "father":
        return TenantPolicy("father", "work", False, False, False, frozenset({"anthropic", "openai"}))
    raise TenantRefused(f"Unknown tenant: {settings.tenant}")


def refuse_training(settings: Settings) -> None:
    if not policy(settings).training_allowed:
        raise TenantRefused(f"tenant {settings.tenant} does not allow training")
