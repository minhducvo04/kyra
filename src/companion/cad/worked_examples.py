"""A small library of worked examples for the describe-it prompt: one per shape family, with the arithmetic written
out, picked for a brief by a deterministic keyword match.

The examples are verified data (src/companion/cad/worked_examples.json, built by scripts/cad_examples_build.py from
the training-data makers with a fixed seed): each reply builds on the real kernel through the real propose loop on
the first attempt and matches no frozen eval part (same words, same numbers or the same built part). The prompt
carries at most one example, and only when `propose(..., worked_example=True)` asks for it.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LIBRARY = Path(__file__).with_name("worked_examples.json")

# family -> (anchor, patterns). The anchor decides whether a family can match at all: {"any": [...]} needs one of
# them, {"all": [...]} needs every one. The score is how many of the family's patterns the brief matches; the
# highest score wins and ties go to the earlier family (the dict order), so the pick never depends on chance.
RULES = {
    "flange": ({"any": [r"\bflange", r"bolt circle", r"\bpcd\b"]},
               [r"\bflange", r"bolt circle", r"bolt holes?", r"\bpcd\b", r"centre bore|center bore"]),
    "hex_standoff": ({"any": [r"\bhex(?:agon(?:al)?)?\b"]},
                     [r"\bhex(?:agon(?:al)?)?\b", r"across (?:the )?(?:flats|corners)", r"\bstandoff", r"\bnut\b", r"\bspacer"]),
    "washer": ({"any": [r"\bwasher", r"\b(?:flat|round) ring"]},
               [r"\bwasher", r"\bring\b", r"(?:outside|outer) diameter", r"(?:inside|inner) diameter"]),
    "l_angle": ({"any": [r"\bangle (?:bracket|iron|section)", r"\bl[- ]?(?:shaped|shape|bracket|angle|profile)"]},
                [r"\bangle (?:bracket|iron|section)", r"\bl[- ]?(?:shaped|shape|bracket|angle|profile)", r"\blegs?\b"]),
    "u_channel": ({"any": [r"\bchannel", r"\btrough"]},
                  [r"\bchannel", r"\bu[- ]?(?:channel|section|shaped)", r"\btrough", r"open (?:at|on) the top", r"\bwalls?\b"]),
    "grooved_pulley": ({"any": [r"\bpulley", r"\bsheave"]},
                       [r"\bpulley", r"\bsheave", r"\bgroove", r"\bwheel\b", r"\bbore\b"]),
    "open_box": ({"any": [r"\btray", r"\bhollow", r"open (?:at|on) the top"]},
                 [r"\btray", r"\bhollow", r"open (?:at|on) the top", r"\bwalls?\b", r"\bbox\b"]),
    "hole_row": ({"any": [r"holes? in a row", r"\brow of\b"]},
                 [r"holes? in a row", r"\brow of\b", r"evenly spaced", r"\bbar\b"]),
    "plate_holes": ({"all": [r"\bplate\b", r"\bholes?\b"]},
                    [r"\bplate\b", r"\bholes?\b", r"each corner|near each corner|nearest edges|from the (?:two )?edges"]),
}


@lru_cache(maxsize=1)
def library() -> dict:
    """{family: {"brief", "reply", "seed"}} from the verified JSON file; {} when the file is missing."""
    try:
        return json.loads(LIBRARY.read_text(encoding="utf-8")).get("examples", {})
    except (OSError, ValueError):
        return {}


def pick(brief: str) -> dict | None:
    """The worked example for the closest family, or None. Pure keyword matching: same brief, same answer."""
    text = (brief or "").lower()
    best, best_score = None, 0
    for family, (anchor, patterns) in RULES.items():
        needs = anchor.get("any") or anchor.get("all") or []
        found = [bool(re.search(p, text)) for p in needs]
        if not (any(found) if "any" in anchor else all(found)):
            continue
        score = sum(bool(re.search(p, text)) for p in patterns)
        if score > best_score:
            best, best_score = family, score
    example = library().get(best) if best else None
    return {"family": best, **example} if example else None


def block(example: dict) -> str:
    """The prompt text for one example."""
    return ("Worked example of a similar part (different sizes). Do the same steps and write the same kind of "
            "arithmetic in assumptions before the spec:\n"
            + json.dumps({"brief": example["brief"], "reply": example["reply"]}, separators=(",", ":")))
