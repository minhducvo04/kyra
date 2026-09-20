"""Initial-request release policy and content-free audit (stage A3a)."""
import json
import logging
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import ClassVar, Literal
from urllib.parse import unquote

from sqlalchemy import Engine, delete, func, insert, select

from companion.db import engine_for_store
from companion.paths import DATA_DIR
from companion.privacy import AssembledContext, ContextBlock, PrivacyClass, Source
from companion.schema import outbound_audit
from companion.settings import get_settings

logger = logging.getLogger(__name__)
Mode = Literal["off", "dry_run", "enforce"]
_prune_lock = threading.Lock()
_next_prune = 0.0
_KEY_SHAPED = re.compile(r"(?:sk-ant-|sk-|ghp_)[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ReleasePolicy:
    grants: frozenset[PrivacyClass]
    version: ClassVar[int] = 1

    def __post_init__(self):
        grants = frozenset(PrivacyClass(name) for name in self.grants)
        forbidden = grants - {PrivacyClass.conversation, PrivacyClass.job_search}
        if forbidden:
            raise ValueError(f"Cannot grant release for: {', '.join(sorted(c.value for c in forbidden))}")
        object.__setattr__(self, "grants", grants)

    def allows(self, classes: frozenset[PrivacyClass]) -> bool:
        return classes <= self.grants


class ReleaseRefused(Exception):
    """A content-free refusal naming only restricted classes or a secret."""

    def __init__(self, classes: frozenset[PrivacyClass] = frozenset(), *, secret: bool = False, completed_tools=()):
        self.classes = frozenset(classes)
        self.secret = bool(secret)
        self.completed_tools = tuple(completed_tools)
        reason = "secret" if secret else ", ".join(sorted(c.value for c in classes))
        super().__init__(f"Outbound release refused: {reason}.")

    def __str__(self):
        from companion.provider import stopped_message

        return super().__str__() + " " + stopped_message(self.completed_tools)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    would_refuse: bool
    refused_blocks: tuple[ContextBlock, ...]


@dataclass(frozen=True)
class AuditRow:
    ts: str
    destination: str
    mode: str
    accepted: bool
    would_refuse: bool
    tier: int
    classes: str
    refused: str
    flags: str
    bytes: int
    request_id: str
    policy_version: int


def _now(now: datetime | None) -> datetime:
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("an aware audit timestamp is required")
    return now.astimezone(UTC)


def _window(days: int, now: datetime | None) -> tuple[str, str]:
    if type(days) is not int or days < 1:
        raise ValueError("days must be a positive integer")
    end = _now(now)
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


class OutboundAudit:
    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @cached_property
    def engine(self) -> Engine:
        if self._engine is not None:
            outbound_audit.create(self._engine, checkfirst=True)
            return self._engine
        return engine_for_store(DATA_DIR / "outbound_audit.db")

    def record(self, row: AuditRow) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(outbound_audit).values(**asdict(row)))
        # Claim maintenance after the commit, globally across gate instances.
        global _next_prune
        with _prune_lock:
            now = time.monotonic()
            if now < _next_prune:
                return
            _next_prune = now + 3600
        try:
            self.prune()
        except Exception as exc:
            logger.warning("Outbound audit maintenance failed: %s", type(exc).__name__)

    def list(self, limit: int | None = None) -> list[AuditRow]:
        query = select(outbound_audit).order_by(outbound_audit.c.id.desc())
        if limit is not None:
            query = query.limit(max(0, limit))
        with self.engine.connect() as conn:
            return [AuditRow(**{k: v for k, v in row.items() if k != "id"}) for row in conn.execute(query).mappings()]


    def prune(self, days: int = 90, now: datetime | None = None) -> int:
        cutoff, _ = _window(days, now)
        with self.engine.begin() as conn:
            return conn.execute(delete(outbound_audit).where(outbound_audit.c.ts < cutoff)).rowcount


class OutboundGate:
    def __init__(self, policy: ReleasePolicy, audit: OutboundAudit, mode: Mode, secrets=()):
        if mode not in {"off", "dry_run", "enforce"}:
            raise ValueError(f"Unknown outbound mode: {mode}")
        self.policy = policy
        self.audit = audit
        self.mode = mode
        self._secrets = tuple(secrets)
        if any(not isinstance(secret, str) or len(secret) < 6 for secret in self._secrets):
            raise ValueError("configured secrets must be strings of at least 6 characters")
        self._secret_patterns = tuple(
            re.compile(re.escape(secret) if len(secret) >= 12 else rf"(?<![^\W_]){re.escape(secret)}(?![^\W_])")
            for secret in self._secrets
        )

    def check(self, context: AssembledContext, destination: str, *, now: datetime | None = None) -> Decision:
        return self._check(context, destination, now=now)

    def preflight(self, context: AssembledContext, destination: str) -> Decision:
        return self._check(context, destination, refusal_only=True)

    def check_request(
        self, rendered_text: str, label, destination: str = "anthropic", *, payload_bytes=None, required_gate=None,
    ) -> Decision:
        context = AssembledContext((ContextBlock(
            rendered_text, "system", Source.system, "provider:request", *label,
        ),))
        return self._check(context, destination, strict_audit=True, payload_bytes=payload_bytes,
                           required_gate=required_gate)

    def _assess(self, context, rendered):
        """Pure policy evaluation, including for a stream's captured gate."""
        if self.mode == "off":
            return Decision(True, False, ()), frozenset(), set()
        refused = tuple(block for block in context.blocks if not self.policy.allows(block.classes))
        secret_found = any(pattern.search(text) for text in rendered for pattern in self._secret_patterns)
        flags = set()
        if any(match.group() not in self._secrets for text in rendered for match in _KEY_SHAPED.finditer(text)):
            flags.add("key_shaped_text")
        if secret_found:
            flags.add("configured_secret")
        denied = frozenset(c for block in refused for c in block.classes) - self.policy.grants
        allowed = not secret_found and (self.mode == "dry_run" or not refused)
        return Decision(allowed, bool(refused) or secret_found, refused), denied, flags

    def _check(
        self, context, destination, *, now=None, refusal_only=False, strict_audit=False,
        payload_bytes=None, required_gate=None,
    ) -> Decision:
        if destination == "local":
            return Decision(True, False, ())
        gates = [self]
        if required_gate is not None and required_gate is not self:
            gates.append(required_gate)
        if all(gate.mode == "off" for gate in gates):
            return Decision(True, False, ())
        fields = ["".join(block.content for block in context.blocks if block.role == "system")]
        fields.extend(block.content for block in context.blocks if block.role != "system")
        rendered = [text for field in fields for text in (field, unquote(field))]
        assessments = [gate._assess(context, rendered) for gate in gates]
        allowed = all(decision.allowed for decision, _, _ in assessments)
        would_refuse = any(decision.would_refuse for decision, _, _ in assessments)
        refused = tuple(block for block in context.blocks
                        if any(block in decision.refused_blocks for decision, _, _ in assessments))
        denied_classes = frozenset(c for _, denied, _ in assessments for c in denied)
        flags = set().union(*(flags for _, _, flags in assessments))
        tier, classes = context.labels()
        if payload_bytes is None:
            payload_bytes = sum(len(field.encode("utf-8")) for field in fields)
        row = AuditRow(
            ts=_now(now).isoformat(), destination=destination, mode=self.mode,
            accepted=allowed, would_refuse=would_refuse, tier=int(tier),
            classes=json.dumps(sorted(c.value for c in classes)),
            refused=json.dumps([
                {
                    "source": block.source.value, "classes": sorted(c.value for c in block.classes),
                    "denied": sorted(c.value for c in block.classes & denied_classes),
                } for block in refused
            ]),
            flags=json.dumps(sorted(flags)), bytes=payload_bytes, request_id=secrets.token_urlsafe(16),
            policy_version=self.policy.version,
        )
        # The entry gate owns the single audit; off never opens its store.
        if self.mode != "off" and (not refusal_only or not allowed):
            try:
                self.audit.record(row)
            except Exception as exc:
                # Exception messages/tracebacks can include bound SQL values. Type only.
                logger.error("Outbound audit failed: %s", type(exc).__name__)
                if strict_audit:
                    from companion.provider import AuditUnavailable

                    raise AuditUnavailable() from None
        if not allowed:
            raise ReleaseRefused(denied_classes, secret="configured_secret" in flags)
        return Decision(allowed, would_refuse, refused)


def report(audit: OutboundAudit, days: int = 30, now: datetime | None = None) -> dict:
    """Aggregate a bounded time window without loading audit payload metadata."""
    cutoff, end = _window(days, now)
    query = select(outbound_audit.c.would_refuse, outbound_audit.c.refused, func.count()).where(
        outbound_audit.c.ts >= cutoff, outbound_audit.c.ts <= end,
    ).group_by(outbound_audit.c.would_refuse, outbound_audit.c.refused)
    by_class, by_source = Counter(), Counter()
    requests = would_refuse = 0
    with audit.engine.connect() as conn:
        for refused, blocks, count in conn.execution_options(yield_per=100).execute(query):
            requests += count
            would_refuse += count if refused else 0
            for block in json.loads(blocks):
                for name in block.get("denied", block["classes"]):
                    by_class[name] += count
                by_source[block["source"]] += count
    return {
        "requests": requests, "would_refuse": would_refuse,
        "by_class": dict(sorted(by_class.items())), "by_source": dict(sorted(by_source.items())),
    }


def default_gate() -> OutboundGate:
    settings = get_settings()
    audit = OutboundAudit()
    gate = OutboundGate(
        ReleasePolicy(settings.release_grants), audit, settings.outbound_gate,
        secrets=tuple(value for value in (
            settings.anthropic_api_key, settings.api_token, settings.vesync_password.get_secret_value(),
        ) if value),
    )
    return gate
