#!/usr/bin/env python3
"""Study approved source moments: metadata and supplied transcripts only."""
import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.paths import DATA_DIR
from companion.reels import (
    MomentProposer,
    ReelsStore,
    adapter_for,
    embed_url,
    render_progress,
)


def _clock(value: str) -> float:
    """m:ss or h:mm:ss, as the transcript panel shows it, or plain seconds."""
    parts = [float(p) for p in value.split(":")]
    if len(parts) > 3 or any(p < 0 for p in parts):
        raise argparse.ArgumentTypeError(f"not a timestamp: {value}")
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def _proposer():
    from anthropic import Anthropic

    from companion.config import require_api_key
    from companion.llm import AnthropicLLM

    return MomentProposer(AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=16000))  # adaptive thinking spends from this budget too: 4000 cut the first real reply (2026-09-16)


def _ask(store, user, moment, kind):
    question = moment.questions["initial" if kind == "delayed" else kind]
    print(question.stem)
    for number, option in enumerate(question.options, 1):
        print(f"  {number}. {option.text}")
    while True:
        chosen = input("Choose 1-4 (or q to stop): ").strip()
        if chosen.lower() == "q":
            return False
        if chosen not in {"1", "2", "3", "4"}:
            print("Enter an option number.")
            continue
        feedback = store.record_attempt(user, moment.id, kind, question.options[int(chosen) - 1].text)
        print(feedback.message)
        print(f"XP +{feedback.xp}; {feedback.mastery}")
        if feedback.correct or feedback.revealed:
            return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="duc")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Register a YouTube URL with a supplied transcript")
    add.add_argument("url")
    add.add_argument("--transcript", required=True, type=Path)
    add.add_argument("--origin", choices=("pasted", "file", "creator"), default="file")
    propose = commands.add_parser("propose")
    propose.add_argument("source_id", type=int)
    propose.add_argument("--max", type=int, default=3, dest="max_moments")
    mark = commands.add_parser("mark")
    mark.add_argument("source_id", type=int)
    mark.add_argument("--start", required=True, type=_clock)
    mark.add_argument("--end", required=True, type=_clock)
    listing = commands.add_parser("list")
    listing.add_argument("source_id", type=int, nargs="?")
    for command in ("show", "approve", "reject", "quiz"):
        sub = commands.add_parser(command)
        sub.add_argument("moment_id", type=int)
        if command == "quiz":
            sub.add_argument("--mode", choices=("quick", "learn"), default="quick")
    commands.add_parser("review")
    commands.add_parser("progress")
    args = parser.parse_args(argv)
    store = ReelsStore()
    try:
        if args.command == "add":
            text = args.transcript.read_text(encoding="utf-8")
            source = adapter_for(args.url).register(args.url)
            source = store.add_source(source.model_copy(update={"transcript_origin": args.origin}), text)
            print(f"Source {source.id}: {source.title}")
        elif args.command in {"propose", "mark"}:
            source = store.get_source(args.source_id)
            if source is None:
                raise ValueError("Unknown source")
            transcript = store.transcript(source.id)
            proposer = _proposer()
            if args.command == "propose":
                result = proposer.propose(source, transcript, args.max_moments, DATA_DIR / "reels/raw")
            else:
                result = proposer.elaborate(source, transcript, args.start, args.end, DATA_DIR / "reels/raw")
            for moment in result.moments:
                saved = store.add_moment(moment)
                print(f"Proposed {saved.id}: {saved.learning_objective}")
            for rejection in result.rejected:
                print(f"Rejected: {rejection.reason}; raw: {rejection.raw_path}")
        elif args.command == "list":
            for moment in store.moments(args.source_id):
                print(f"{moment.id}: {moment.status}: {moment.learning_objective}")
        elif args.command == "progress":
            print(render_progress(store.progress(args.user, week_ending=datetime.now(UTC).date())))
        elif args.command == "review":
            due = store.due(args.user)
            if not due:
                print("No reviews due.")
            for moment in due:
                print(moment.learning_objective)
                if not _ask(store, args.user, moment, "delayed"):
                    break
        else:
            moment = store.get_moment(args.moment_id)
            if moment is None:
                raise ValueError("Unknown moment")
            if args.command in {"approve", "reject"}:
                store.set_status(moment.id, "approved" if args.command == "approve" else "rejected")
                print(f"Moment {moment.id}: {store.get_moment(moment.id).status}")
            elif args.command == "show":
                source = store.get_source(moment.source_id)
                print(embed_url(source, moment.start_s, moment.end_s) or source.url)
                print(moment.model_dump_json(indent=2))
            elif args.command == "quiz":
                # Enforce the gate before showing even the question or card.
                if moment.status != "approved":
                    raise ValueError("Only approved moments can be studied")
                source = store.get_source(moment.source_id)
                if source.rights_state == "REJECTED":
                    raise ValueError("Source is rejected")
                print(embed_url(source, moment.start_s, moment.end_s) or source.url)
                if args.mode == "learn":
                    input("Watch the moment, then pause and press Enter for the prediction question. ")
                    store.record_watch(args.user, moment.id)
                if _ask(store, args.user, moment, "initial") and args.mode == "learn":
                    print(json.dumps(moment.concept_card.firewall_view(), indent=2))
                    _ask(store, args.user, moment, "transfer")
    except (ValueError, OSError, URLError) as exc:
        parser.exit(1, f"reels: {exc}\n")
    except (EOFError, KeyboardInterrupt):
        parser.exit(0, "\nStopped.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
