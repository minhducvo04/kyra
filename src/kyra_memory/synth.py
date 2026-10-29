"""Synthetic tenants for scale tests: python -m kyra_memory.synth OUT --seed 1 --tenants 5.

Names and sentences come from fixed word lists and one seeded generator, so a seed always writes the same bytes.
Output goes only under OUT, which must be missing or empty.
"""
import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kyra_memory.labels import PrivacyClass, Tier
from kyra_memory.sqlite_backend import SqliteBackend

SCOPE = "synth"
LABEL = (Tier.T2, frozenset({PrivacyClass.conversation}))
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SUBJECTS = ("boiler", "invoice", "ladder", "van", "quote", "permit", "gutter", "thermostat", "contract", "roster")
VERBS = ("needs", "replaces", "schedules", "approves", "inspects", "reorders", "postpones", "prefers")
OBJECTS = ("copper pipe", "weekly visit", "spare key", "site photo", "signed form", "brass valve", "second crew",
           "morning slot", "fixed price", "parts order")
MODALS = ("must", "should never", "always", "may not", "should first")
ACTIONS = ("confirm the address", "attach the photo", "ask before ordering", "log the visit", "quote in writing",
           "call the customer back", "check the permit", "send the invoice")


class SynthError(ValueError):
    """The output path or a size is not acceptable."""


def generate(out: Path | str, *, seed: int = 1, tenants: int = 3, users: int = 4, agents: int = 2, facts: int = 20,
             rules: int = 5) -> dict:
    """Write `tenants` tenants under `out` and return the manifest, which is also saved as manifest.json."""
    out = Path(out)
    if min(tenants, users, agents) < 1 or min(facts, rules) < 0:
        raise SynthError("tenants, users and agents must be at least 1")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SynthError(f"{out} is not an empty directory")
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    tick = iter(range(10**9))
    backend = SqliteBackend(out, SCOPE, clock=lambda: (EPOCH + timedelta(seconds=next(tick))).isoformat())
    manifest = {"seed": seed, "tenants": {}}
    try:
        for t in range(1, tenants + 1):
            tenant = f"tenant-{t:04d}"
            backend.open_tenant(tenant, create=True)
            for _ in range(facts):
                user = f"user-{rng.randint(1, users):04d}"
                subject, verb = rng.choice(SUBJECTS), rng.choice(VERBS)
                body = f"{user} {verb} {rng.choice(OBJECTS)} for the {subject}"
                day = (EPOCH + timedelta(days=rng.randint(0, 300))).isoformat()
                backend.add(tenant, "facts", body, source=f"synth:seed{seed}", topic=verb,
                            key=f"{user}|{subject}|{verb}", event_time=day, label=LABEL)
            for _ in range(rules):
                agent = f"agent-{rng.randint(1, agents):02d}"
                body = f"{agent} {rng.choice(MODALS)} {rng.choice(ACTIONS)}"
                backend.add(tenant, "rules", body, source=f"synth:seed{seed}", topic="policy",
                            key=f"{agent}|{body}", label=LABEL)
            manifest["tenants"][tenant] = {"users": users, "agents": agents, **backend.counts(tenant)}
    finally:
        backend.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kyra_memory.synth", description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path, help="a missing or empty directory")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--tenants", type=int, default=3)
    p.add_argument("--users", type=int, default=4)
    p.add_argument("--agents", type=int, default=2)
    p.add_argument("--facts", type=int, default=20)
    p.add_argument("--rules", type=int, default=5)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = generate(args.out, seed=args.seed, tenants=args.tenants, users=args.users, agents=args.agents,
                            facts=args.facts, rules=args.rules)
    except SynthError as e:
        print(f"refused: {e}")
        return 2
    print(f"wrote {len(manifest['tenants'])} tenants under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
