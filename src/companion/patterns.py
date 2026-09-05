"""Usage-pattern analysis over data/router.log - v1 of the idea Duc
proposed 2026-09-03 (see docs/agentic-roadmap.md, "Usage-pattern
observation"): notice repeated tool usage and surface a suggestion, so
Duc doesn't have to notice it himself before deciding something's worth
automating. The real example that prompted this: he asked Kyra for
resume/cover-letter help multiple times before Job Application Auto
existed - the repetition itself was the signal a pipeline was worth
building.

Deliberately lean, matching this project's "start cheap, prove it's
useful" pattern (RSS not scraping, draft-only handoff): count tool-path
reasons in router.log over a rolling window, flag anything crossing a
threshold, and only on an actual flagged hit spend one real Claude call
turning the raw count into a plain-language observation. text-path
reasons are skipped entirely for v1 - "casual chat" and "explaining a
topic" dominate that path and aren't a meaningful signal, while
tool-path reasons are already specific (e.g. "draft tailored job
application material") and repetition there is exactly the kind of
thing worth flagging.

Not automatic, not scheduled by this module - see scripts/analyze_
patterns.py for how it's actually run. A v2 that reasons over usage
instead of just counting it is sketched in the roadmap doc but not
built - this stays pure counting on purpose.
"""
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from companion.llm import AnthropicLLM
from companion.paths import DATA_DIR

LOG_PATH = DATA_DIR / "router.log"
OBSERVATIONS_PATH = DATA_DIR / "pattern_observations.md"

DEFAULT_WINDOW_DAYS = 7
DEFAULT_THRESHOLD = 3

OBSERVATION_SYSTEM = (
    "You write short, useful observations about a person's own tool usage, meant to be read by them "
    "later - plain, specific, no filler, no flattery."
)
OBSERVATION_PROMPT = """Duc used a tool for this exact reason {count} times in the last {days} days: "{reason}"

Write one short paragraph (2-4 sentences) noting this pattern and suggesting, concretely, what might be worth \
building or changing to make this easier - be specific to what this particular pattern actually suggests, not \
generic productivity advice. If the count doesn't obviously point to anything beyond "this is a thing Duc does \
often," say that plainly instead of forcing a suggestion that isn't there."""


@dataclass
class PatternHit:
    reason: str
    count: int
    window_days: int


def load_recent_tool_reasons(window_days: int = DEFAULT_WINDOW_DAYS, log_path: Path | str = LOG_PATH) -> "list[str]":
    path = Path(log_path)
    if not path.exists():
        return []
    cutoff = datetime.now(UTC).timestamp() - window_days * 86400
    reasons = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("ts", 0) < cutoff:
            continue
        if rec.get("path") != "tool":
            continue
        reason = rec.get("reason")
        if reason:
            reasons.append(reason)
    return reasons


def find_patterns(
    window_days: int = DEFAULT_WINDOW_DAYS, threshold: int = DEFAULT_THRESHOLD, log_path: Path | str = LOG_PATH
) -> "list[PatternHit]":
    counts = Counter(load_recent_tool_reasons(window_days, log_path))
    return [
        PatternHit(reason=reason, count=count, window_days=window_days)
        for reason, count in counts.most_common()
        if count >= threshold
    ]


def write_observation(hit: PatternHit, llm: AnthropicLLM, path: Path | str = OBSERVATIONS_PATH) -> str:
    """One real Claude call, only for an already-flagged hit - not run
    per-candidate-reason, per-turn, or on any schedule this module
    controls itself.
    """
    text = llm.respond(
        system=OBSERVATION_SYSTEM, history=[],
        user_input=OBSERVATION_PROMPT.format(count=hit.count, days=hit.window_days, reason=hit.reason),
    ).strip()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    with path.open("a", encoding="utf-8") as f:
        if is_new:
            f.write("# Pattern observations\n\nAppend-only, dated - most recent entries are at the bottom.\n\n")
        f.write(f"## [{date_str}] \"{hit.reason}\" - {hit.count}x in {hit.window_days} days\n\n{text}\n\n")
    return text
