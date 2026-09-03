"""Notice repeated tool usage in Kyra's router log and surface an
observation - Duc's idea from 2026-09-03 (see docs/agentic-roadmap.md,
"Usage-pattern observation"): if he's asked Kyra to do the same kind of
thing several times, that's worth him knowing about, even before he'd
think to build a dedicated pipeline for it himself. Job Application
Auto is the real example that prompted this - he asked for resume help
multiple times before that existed.

Usage:
    python3 scripts/analyze_patterns.py                  # last 7 days, threshold 3
    python3 scripts/analyze_patterns.py --days 30 --threshold 5

Meant to be run manually for now (or via a future launchd/cron job -
see the roadmap doc), not triggered automatically per-turn - each
flagged pattern costs one real Claude call, so this should be an
occasional check, not something that runs on every message.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anthropic import Anthropic

from companion.config import require_api_key
from companion.llm import AnthropicLLM
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.patterns import DEFAULT_THRESHOLD, DEFAULT_WINDOW_DAYS, find_patterns, write_observation


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS, help="rolling window size in days")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="minimum occurrences to flag")
    args = parser.parse_args()

    hits = find_patterns(window_days=args.days, threshold=args.threshold)
    if not hits:
        print(f"No repeated tool usage crossing {args.threshold}x in the last {args.days} days. Nothing to report.")
        return

    llm = AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=300)
    notes = MarkdownMemoryNotesStore()

    print(f"Found {len(hits)} pattern(s) worth noting:\n")
    for hit in hits:
        observation = write_observation(hit, llm)
        notes.add(
            "observations",
            f"Pattern noticed: \"{hit.reason}\" - {hit.count}x in {hit.window_days} days. {observation}",
        )
        print(f"[{hit.count}x] {hit.reason}\n  {observation}\n")

    print("Saved to data/pattern_observations.md and memory_notes (category: observations) - "
          "Kyra can bring this up naturally next conversation.")


if __name__ == "__main__":
    main()
