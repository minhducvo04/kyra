"""Decision metrics (RQ51): score a small decision model against the path that decides today.

A row is (probs, choice, truth): probs is {option: p} from the model, or None when it gave no answer (no worker,
timeout, refusal); choice is the model's pick (None when probs is None); truth is the accepted answer (the live
path's choice, or a later outcome that names the right option). The model "covers" a row when it would act:
top >= t and top - second >= m. Everything it does not cover escalates to today's path.

Pure and stdlib only (the frozen and dev sets under tests/data are read, never written). Thresholds picked here are
data for data/decisions/thresholds.json; nothing here writes them.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

Row = tuple  # (probs: dict[str, float] | None, choice: str | None, truth: str)

# Go bars from the decision-model plan (c). Only the log-measurable bars live here; rule
# cases, the health probe and the frozen suites are checked by their own tests.
BARS = {
    "home_router": {"agreement": 0.90, "coverage": 0.70, "ece": 0.08, "min_decisions": 200},
    "lead_route": {"agreement": 0.90, "coverage": 0.60, "ece": 0.10, "min_decisions": 200},
}


def _top2(probs: dict) -> tuple[float, float]:
    vals = sorted(probs.values(), reverse=True)
    return vals[0], (vals[1] if len(vals) > 1 else 0.0)


def confidence(row: Row) -> float | None:
    return None if not row[0] else _top2(row[0])[0]


def covered(row: Row, t: float, m: float = 0.0) -> bool:
    if not row[0] or row[1] is None:
        return False
    top, second = _top2(row[0])
    return top >= t and top - second >= m


def agreement(rows: list) -> float | None:
    """Share of rows where the model's choice equals the truth; None for no rows."""
    return sum(1 for _, c, y in rows if c == y) / len(rows) if rows else None


def at_threshold(rows: list, t: float, m: float = 0.0) -> dict:
    """Coverage, agreement on covered rows, abstain rate and calls saved at one (t, m)."""
    kept = [r for r in rows if covered(r, t, m)]
    n = len(rows)
    return {"t": t, "m": m, "n": n, "covered": len(kept),
            "coverage": len(kept) / n if n else 0.0,
            "agreement": agreement(kept),
            "abstain_rate": (n - len(kept)) / n if n else 0.0,
            # each covered row is one call the escalation path (27B, 14B, Opus) no longer makes
            "calls_saved": len(kept)}


def ece(rows: list, bins: int = 10) -> float | None:
    """Expected calibration error of the top probability over rows with probs, equal-width bins (lo, hi]."""
    scored = [(confidence(r), r[1] == r[2]) for r in rows if r[0] and r[1] is not None]
    if not scored:
        return None
    err = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [(c, ok) for c, ok in scored if lo < c <= hi or (b == 0 and c == 0)]
        if group:
            conf = sum(c for c, _ in group) / len(group)
            acc = sum(1 for _, ok in group if ok) / len(group)
            err += len(group) / len(scored) * abs(conf - acc)
    return err


def risk_coverage(rows: list, m: float = 0.0) -> list:
    """One line per distinct confidence, highest first: acting at t = that confidence gives this coverage and risk."""
    out = []
    for t in sorted({confidence(r) for r in rows if r[0] and r[1] is not None}, reverse=True):
        s = at_threshold(rows, t, m)
        out.append({"t": t, "coverage": s["coverage"], "risk": None if s["agreement"] is None else 1 - s["agreement"]})
    return out


def pick_threshold(rows: list, target_coverage: float, m: float = 0.0) -> float | None:
    """The highest t whose coverage reaches the target, or None when no t can (too many rows without an answer).

    Monotonic: a higher target never returns a higher t.
    """
    if not 0 < target_coverage <= 1:
        raise ValueError("target_coverage must be in (0, 1]")
    for line in risk_coverage(rows, m):  # highest t first, coverage only grows as t falls
        if line["coverage"] >= target_coverage - 1e-12:
            return line["t"]
    return None


def agreement_at_coverage(rows: list, target_coverage: float, m: float = 0.0) -> dict:
    t = pick_threshold(rows, target_coverage, m)
    if t is None:
        return {"t": None, "m": m, "n": len(rows), "covered": 0, "coverage": 0.0, "agreement": None,
                "abstain_rate": 1.0, "calls_saved": 0}
    return at_threshold(rows, t, m)


def report(rows: list, family: str, target_coverage: float | None = None, m: float = 0.0) -> dict:
    """Every metric plus the go/no-go line against the family's PLAN.md bar."""
    bar = BARS.get(family)
    target = target_coverage or (bar["coverage"] if bar else 0.7)
    pick = agreement_at_coverage(rows, target, m)
    cal = ece(rows)
    out = {"family": family, "n": len(rows), "target_coverage": target,
           "no_answer": sum(1 for r in rows if not r[0]), "ece": cal, "pick": pick,
           "suggested_thresholds": None if pick["t"] is None else {"t": pick["t"], "m": m},
           "risk_coverage": risk_coverage(rows, m), "bar": bar}
    out["go"], out["go_line"] = verdict(out)
    return out


def verdict(rep: dict) -> tuple[bool, str]:
    bar, pick, cal = rep["bar"], rep["pick"], rep["ece"]
    if bar is None:
        return False, f"NO-GO {rep['family']}: no PLAN.md bar for this family"
    fails = []
    if rep["n"] < bar["min_decisions"]:
        fails.append(f"decisions {rep['n']} < {bar['min_decisions']}")
    if pick["agreement"] is None or pick["agreement"] < bar["agreement"]:
        fails.append(f"agreement {_pct(pick['agreement'])} < {_pct(bar['agreement'])}")
    if pick["coverage"] < bar["coverage"]:
        fails.append(f"coverage {_pct(pick['coverage'])} < {_pct(bar['coverage'])}")
    if cal is None or not math.isfinite(cal) or cal > bar["ece"]:
        fails.append(f"ECE {_num(cal)} > {bar['ece']}")
    head = (f"{rep['family']}: agreement {_pct(pick['agreement'])} at coverage {_pct(pick['coverage'])} "
            f"(t={_num(pick['t'])}), ECE {_num(cal)}, abstain {_pct(pick['abstain_rate'])}, "
            f"calls saved {pick['calls_saved']}/{rep['n']}")
    return (not fails), ("GO " + head if not fails else "NO-GO " + head + "; fails: " + "; ".join(fails))


# ---- live gate (D1): a head goes live only where it beats the rules on the frozen sets -----------------------------
# The threshold is picked on the family's dev set and measured once on the frozen sets (never tuned on them).
# Row shapes: head rows are (probs | None, choice | None, truth); rules rows are (choice | None, truth), aligned.
# `truth` is a node id or ASK; a choice of None means "abstain", which is right exactly on ASK rows.

ASK = "ask"
HEAD_FAMILIES = ("home_router", "assign", "recovery", "test_select")
_TESTS_DATA = Path(__file__).resolve().parents[2] / "tests" / "data"
FROZEN = {"home_router": ("home_router_utterances.jsonl", "home_router_cases.jsonl",
                          "home_router_holdout2.jsonl", "home_router_holdout3.jsonl")}
DEV = {"home_router": "home_router_dev.jsonl"}
LIVE_BAR = {"min_covered": 20, "max_ask_acted": 0.10, "ece": 0.10}


def labelled_rows(path: Path) -> list:
    """(text, truth) per row of a frozen or dev file; header rows are skipped; null or missing truth reads as ASK."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "_header" in row or not isinstance(row.get("text"), str):
            continue
        truth = row.get("expect_node", row.get("expected"))
        out.append((row["text"], truth if isinstance(truth, str) and truth else ASK))
    return out


def heldout_rows(family: str, root: Path = _TESTS_DATA) -> list:
    return [r for name in FROZEN.get(family, ()) for r in labelled_rows(root / name)]


def dev_rows(family: str, root: Path = _TESTS_DATA) -> list:
    return labelled_rows(root / DEV[family]) if family in DEV else []


def _acted(choice) -> str:
    return choice if choice is not None else ASK


def live_verdict(family: str, head_dev: list, head_frozen: list, rules_frozen: list,
                 target_coverage: float = 0.7, m: float = 0.0) -> dict:
    """Live or shadow for one family, with every number the verdict rests on.

    t is the highest threshold reaching `target_coverage` on the answerable dev rows. On the frozen rows the head
    acts where covered(t, m) and abstains elsewhere; the rules answer every row. The head goes live only when, on
    the rows it would act on, it agrees with the truth more often than the rules do, acts on at most
    LIVE_BAR["max_ask_acted"] of the ASK rows, is calibrated (ECE) and covers at least LIVE_BAR["min_covered"] rows.
    """
    if len(head_frozen) != len(rules_frozen):
        raise ValueError("head and rules rows must align")
    n = len(head_frozen)
    out = {"family": family, "n": n, "dev_n": len(head_dev), "target_coverage": target_coverage, "m": m,
           "t": None, "covered": 0, "coverage": 0.0, "rules_acc": None, "head_acc": None,
           "head_covered_agreement": None, "rules_covered_acc": None, "ask_rows": sum(1 for _, y in rules_frozen if y == ASK),
           "ask_acted": 0, "ece": None, "no_answer": sum(1 for r in head_frozen if not r[0]), "live": False,
           "bar": dict(LIVE_BAR)}
    if n:
        out["rules_acc"] = sum(1 for c, y in rules_frozen if _acted(c) == y) / n
    t = pick_threshold([r for r in head_dev if r[2] != ASK], target_coverage, m) if head_dev else None
    if t is None:
        out["reason"] = "shadow: no head answers on the dev set, so no threshold"
        return out
    out["t"] = t
    kept = [i for i, r in enumerate(head_frozen) if covered(r, t, m)]
    acted = set(kept)
    out["covered"] = len(kept)
    out["coverage"] = len(kept) / n if n else 0.0
    out["head_acc"] = sum(1 for i, r in enumerate(head_frozen) if (r[1] if i in acted else ASK) == r[2]) / n if n else None
    out["head_covered_agreement"] = agreement([head_frozen[i] for i in kept])
    out["rules_covered_acc"] = (sum(1 for i in kept if _acted(rules_frozen[i][0]) == rules_frozen[i][1]) / len(kept)
                                if kept else None)
    out["ask_acted"] = sum(1 for i in kept if head_frozen[i][2] == ASK)
    out["ece"] = ece(head_frozen)
    fails = []
    if len(kept) < LIVE_BAR["min_covered"]:
        fails.append(f"covered {len(kept)} < {LIVE_BAR['min_covered']}")
    if out["head_covered_agreement"] is None or out["rules_covered_acc"] is None \
            or out["head_covered_agreement"] <= out["rules_covered_acc"]:
        fails.append(f"head {_pct(out['head_covered_agreement'])} does not beat rules {_pct(out['rules_covered_acc'])} on covered rows")
    if out["ask_rows"] and out["ask_acted"] / out["ask_rows"] > LIVE_BAR["max_ask_acted"]:
        fails.append(f"acted on {out['ask_acted']}/{out['ask_rows']} ask rows")
    if out["ece"] is None or not math.isfinite(out["ece"]) or out["ece"] > LIVE_BAR["ece"]:
        fails.append(f"ECE {_num(out['ece'])} > {LIVE_BAR['ece']}")
    out["live"] = not fails
    head = (f"head {_pct(out['head_covered_agreement'])} vs rules {_pct(out['rules_covered_acc'])} on "
            f"{len(kept)}/{n} covered rows (t={_num(t)}, m={_num(m)}), ECE {_num(out['ece'])}")
    out["reason"] = ("live: " + head) if not fails else ("shadow: " + head + "; fails: " + "; ".join(fails))
    return out


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _num(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"
