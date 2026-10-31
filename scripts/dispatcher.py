#!/usr/bin/env python3
"""Pull up to three ready feature jobs per tick; never call a model directly.

Each tick also advances escalation ladders on open team case files (E19).
"""

import argparse
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import lane_send
from companion.feature_team.dispatch import tick


def main_checkout():
    common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], check=True, capture_output=True, text=True)
    return Path(common.stdout.strip()).parent


def channel_target(channel):
    match = re.fullmatch(r"codex-(\d+)", channel)
    return ("codex", match.group(1)) if match else (channel.partition(":")[0], channel.partition(":")[2])


def codex_settings(item, data=None):
    _, target = channel_target(item["channel"])
    folder = Path(item["worktree"]).expanduser().resolve() if item.get("worktree") else lane_send.codex_worktree(target)
    model = item.get("model") or {"Codex Sol": "gpt-6-sol", "Codex Astra": "gpt-6-astra"}.get(
        item.get("by"), lane_send.CODEX_EXEC_MODEL)
    model = {"codex-light": "gpt-6-sol", "codex-astra": "gpt-6-astra"}.get(model, model)
    effort = lane_send.codex_planned_effort(item.get("effort"), item.get("task_kind", "build"), model, data=data)
    final = folder / ".lane-send" / f"dispatcher-{uuid.uuid4().hex}.final.md"
    return folder, model, effort, final


# lane_send's own SystemExit words when it exits before launching anything (oversize brief, bad worktree, effort,
# empty prompt), matched at the start of its output only; a launch that then failed ("send FAILED", carrying the
# worker's log tail) or printed a start receipt stays UNCERTAIN (review of 5fd2e893)
PRE_LAUNCH = re.compile(r"\A\s*(?:refusing an oversize brief|[\w-]+ send NOT STARTED)")
LAUNCHED = re.compile(r"send FAILED|started pid|(?<!NOT )STARTED:")


def refused(response):
    """'NOT STARTED: <reason>' when lane_send refused before any launch, else None. Night 2026-10-17: every codex
    node brief was refused as oversize, this branch answered UNCERTAIN, and the journal held codex:0 and codex:1 for
    30 minutes, expired, resent and was refused again, for hours with no worker and no visible reason."""
    raw = response.stdout + response.stderr
    text = " ".join(raw.split())
    if response.returncode and PRE_LAUNCH.search(raw) and not LAUNCHED.search(raw):
        return "NOT STARTED: " + text[-300:]
    return None


def send(item, *, data=None):
    kind, target = channel_target(item["channel"])
    if kind not in {"codex", "cursor", "grok", "local"} or not target or target.lower() == "owner":
        return "NOT STARTED: no supported lane_send target"
    if kind == "codex":
        if item.get("message", "").startswith("Context hygiene:"):
            # The A6 router stores this receipt in its hygiene journal and routes it to the Board.
            return "NOT STARTED: skipped: codex app threads are off"
        try:
            folder, model, effort, final = codex_settings(item, data)
        except SystemExit as exc:
            return str(exc)
        command = [sys.executable, str(ROOT / "scripts/lane_send.py"), "codex-exec", str(folder), "--no-claim",
                   "--model", model, "--effort", effort, "--out", str(final)]
        if "message" in item:
            command += ["--message", item["message"]]
        else:
            brief_command = [sys.executable, str(ROOT / "scripts/rq_brief.py"), "--node", item["feature"], item["node"],
                             "--channel", f"codex:{target}", "--model", model, "--effort", effort,
                             "--worktree", str(folder)]
            if data is not None:
                brief_command += ["--data-dir", str(data)]
            brief = subprocess.run(brief_command, capture_output=True, text=True, timeout=30, check=True)
            # rq_brief --node prints the contract JSON followed by its human Markdown.
            path = next((Path(line) for line in brief.stdout.splitlines()
                         if line.endswith(".md") and Path(line).is_file()), None)
            if path is None:
                return "NOT STARTED: no rendered brief"
            command += ["--file", str(path)]
        response = subprocess.run(command, capture_output=True, text=True, timeout=120)
        return refused(response) or ("UNCERTAIN" if response.returncode else response.stdout + response.stderr)
    if "message" in item:
        command = [sys.executable, str(ROOT / "scripts/lane_send.py"),
                   kind, target, "--no-claim", "--message", item["message"]]
    else:
        brief = subprocess.run([sys.executable, str(ROOT / "scripts/rq_brief.py"),
                                "--node", item["feature"], item["node"]],
                               capture_output=True, text=True, timeout=30, check=True)
        # The same two-path output (contract .json, then the .md brief) as the codex-exec branch above.
        path = next((Path(line) for line in brief.stdout.splitlines()
                     if line.endswith(".md") and Path(line).is_file()), None)
        if path is None:
            return "NOT STARTED: no rendered brief"
        command = [sys.executable, str(ROOT / "scripts/lane_send.py"),
                   kind, target, "--no-claim", "--file", str(path)]
        if kind in {'codex', 'grok'} and item.get('effort'):
            command += ['--effort', item['effort']]
    if kind == "local":
        from companion.feature_team.dispatch import send_local
        text = item["message"] if "message" in item else path.read_text()
        return send_local(item, text, data or Path(os.environ.get("KYRA_DATA_DIR", main_checkout() / "data")))
    response = subprocess.run(command, capture_output=True, text=True, timeout=120)
    text = response.stdout + response.stderr
    if reason := refused(response):
        return reason
    if response.returncode:
        # A failed courier may already have delivered: hold for Board reconciliation.
        return "UNCERTAIN"
    return text


def dry_run_commands(data, result):
    """Preview the exec argv without rendering briefs or writing to the live data root."""
    from companion.feature_team import dispatch, fixback

    if result.get("stop_reasons") or not result["planned"]:
        return []
    if "autopilot" in result:
        plan = result["autopilot"].get("slots", [])
    else:
        # Legacy ticks expose only a count. Reuse their pure selectors for the preview.
        features = dispatch.snapshot(data)
        dispatch.containment(data, features)
        defaults = {(dispatch.role(w["role"]), w["channel"]) for f in features for w in f["team"]
                    if dispatch.role(w["role"]) in {"engineer", "intern"} and dispatch.allowed(w["channel"])}
        table = dispatch.read(data / "team/dispatch-table.json", [dict(role=r, channel=c, prefers=[])
                              for r, c in sorted(defaults, key=lambda pair: pair[1])])
        from companion.feature_team.muse import dispatch_table
        table = dispatch_table(table, data)
        _, excluded = fixback.route(data, features, table, None, time.time, True)
        _, hygiene, _ = dispatch.route_hygiene(data, table, None, dry_run=True)
        modes = {}
        pauses = dispatch.budget_pauses(data / "team/model-budget.json", time.time(), modes)
        from companion.feature_team import picker
        ranked = picker.rank([r for r in table if r["channel"] not in excluded | hygiene], features,
                             data / "team/rollback.json")  # R14: the same order as a live tick
        eligible = dispatch.budget_table(ranked, pauses, {}, modes)
        watch = dispatch.read(data / "team/lane-watch.json", {})
        lanes = watch.get("lanes", [])
        if "observed_at" in watch and time.time() - watch["observed_at"] > 600:
            lanes = []
        plan = dispatch.select(eligible, lanes, features, dispatch.read(data / "team/dispatcher-pending.json", {}))
    commands = []
    for item in plan:
        if channel_target(item["channel"])[0] != "codex":
            continue
        try:
            folder, model, effort, final = codex_settings(item, data)
            argv = lane_send.codex_exec_argv(folder, f"<brief {item['feature']}:{item['node']}>", effort, model, final)
            commands.append({**item, "command": shlex.join(argv)})
        except SystemExit as exc:
            commands.append({**item, "refusal": str(exc)})
    return commands


def write_plist(main):
    path = main / "data/launchd/com.kyra.dispatcher.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"Label": "com.kyra.dispatcher", "StartInterval": 120,
               "ProgramArguments": [str(main / ".venv/bin/python"),
                                    str(main / "scripts/dispatcher.py"), "tick"],
               "WorkingDirectory": str(main)}
    path.write_bytes(plistlib.dumps(payload))
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["tick", "plist"], nargs="?", default="tick")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    root = main_checkout()
    if args.action == "plist":
        if args.dry_run:
            parser.error("plist writes a file; use tick --dry-run for read-only planning")
        print(write_plist(root))
        return 0
    data = args.data_dir or Path(os.environ.get("KYRA_DATA_DIR", root / "data"))
    inbox = data / 'team/board-inbox.jsonl'
    before = inbox.stat() if inbox.exists() else None
    result = tick(data, sender=lambda item: send(item, data=data), dry_run=args.dry_run)
    if args.dry_run:
        result["commands"] = dry_run_commands(data, result)
    print(json.dumps(result, sort_keys=True))
    if not args.dry_run and inbox.exists() and inbox.stat() != before:
        from done_md import refresh
        refresh(root, data_dir=data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
