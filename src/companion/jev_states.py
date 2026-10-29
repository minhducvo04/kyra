"""Pure Jev projections: opaque identifiers and counts, never source text.

Identifiers are hashed even when a caller accidentally supplies prose as an id.
Unknown fields are ignored. Inputs are mappings except lanes/diff_files (lists).
"""
import hashlib


def _id(value):
    if isinstance(value, dict):
        value = value.get("id", value.get("node", value.get("channel", "")))
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:16]


def _count(value):
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def assign_state(node, lanes, budget):
    return {"node_id": _id(node), "dependency_count": _count(node.get("inputs")),
            "lane_ids": [_id(lane) for lane in lanes], "lane_count": len(lanes),
            "budget_family_count": len(budget),
            "eco_count": sum(isinstance(v, dict) and v.get("mode") == "eco" for v in budget.values()),
            "paused_count": sum(isinstance(v, dict) and "paused_until" in v for v in budget.values())}


def recovery_state(case_row):
    attempt = case_row.get("attempt", 0)
    return {"case_id": _id(case_row), "node_id": _id(case_row.get("node")),
            "attempt": max(0, attempt) if type(attempt) is int else 0,
            "failure_count": _count(case_row.get("failures"))}


def context_state(task, candidate_source):
    return {"task_id": _id(task), "source_id": _id(candidate_source),
            "dependency_count": _count(task.get("inputs"))}


def test_state(diff_files, test_file):
    return {"diff_ids": [_id(p) for p in diff_files], "diff_count": len(diff_files),
            "test_id": _id(test_file)}


def validate(family, state):
    """Refuse anything outside the exact text-free builder schema."""
    import re
    schemas = {
        "assign": ({"node_id"}, {"lane_ids"}, {"dependency_count", "lane_count", "budget_family_count", "eco_count", "paused_count"}),
        "recovery": ({"case_id", "node_id"}, set(), {"attempt", "failure_count"}),
        "context_select": ({"task_id", "source_id"}, set(), {"dependency_count"}),
        "test_select": ({"test_id"}, {"diff_ids"}, {"diff_count"}),
    }
    if family not in schemas or type(state) is not dict:
        raise ValueError("invalid Jev state")
    ids, lists, counts = schemas[family]
    def identifier(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value)
    if (set(state) != ids | lists | counts
            or any(not identifier(state[k]) for k in ids)
            or any(type(state[k]) is not list or any(not identifier(v) for v in state[k]) for k in lists)
            or any(type(state[k]) is not int or state[k] < 0 for k in counts)):
        raise ValueError("invalid Jev state")
