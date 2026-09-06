"""Distill the tool-calling Agent Specialist into a local model (see companion/agent_ft.py for the why).

    python3 scripts/tool_ft.py gen   [--per-category 40]                    # Claude -> data/tool_ft/messages.jsonl
    python3 scripts/tool_ft.py trace [--limit 40] [--categories a,b]        # Claude + fake registry -> traces.jsonl (+ rejected.jsonl)
    python3 scripts/tool_ft.py build [--name full]                          # -> data/tool_ft/data-<name>/{train,valid}.jsonl
    python3 scripts/tool_ft.py train --name qwen7b --model mlx-community/Qwen2.5-7B-Instruct-4bit --data full --iters 400
    python3 scripts/tool_ft.py eval  [--teacher] [--zero-shot <repo>] [--adapter <name>:<repo>] [--limit N]
"""
import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.agent_ft import (
    FT_DIR,
    STUDENT_REPO,
    SYSTEM_PROMPTS,
    Trace,
    evaluate,
    generate_messages,
    history_for,
    load_suite,
    random_today,
    record_trace,
    render_report,
    row_token_lengths,
    split_traces,
    trace_is_clean,
    write_mlx_dataset,
)
from companion.logging_setup import configure_logging
from companion.paths import PROJECT_ROOT

MESSAGES = FT_DIR / "messages.jsonl"
TRACES = FT_DIR / "traces.jsonl"
REJECTED = FT_DIR / "rejected.jsonl"


def _teacher(max_tokens: int = 2000):
    from anthropic import Anthropic

    from companion.config import require_api_key
    from companion.llm import AnthropicLLM

    return AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=8000, tool_max_tokens=max_tokens)


def _schemas():
    from companion.default_tools import default_tool_registry

    # The draft tool never runs in the harness; a placeholder backend keeps the registry buildable without a key.
    return default_tool_registry(draft_backend=object()).schemas()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_gen(args):
    cats = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None
    rows = generate_messages(_teacher(), args.per_category, [c.message for c in load_suite()], categories=cats)
    FT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    with MESSAGES.open(mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} messages -> {MESSAGES} ({mode})")


def cmd_trace(args):
    teacher, schemas = _teacher(), _schemas()
    rows = _read_jsonl(MESSAGES)
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",")}
        rows = [r for r in rows if r["category"] in wanted]
    done = {r["message"] for r in _read_jsonl(TRACES)} | {r["message"] for r in _read_jsonl(REJECTED)} if TRACES.exists() else set()
    rows = [r for r in rows if r["message"] not in done]
    rng = random.Random(args.seed)
    rng.shuffle(rows)  # the file is grouped by category; a --limit pilot should sample across them
    if args.limit:
        rows = rows[: args.limit]
    kept = dropped = 0
    with TRACES.open("a", encoding="utf-8") as out, REJECTED.open("a", encoding="utf-8") as rej:
        for i, r in enumerate(rows, 1):
            trace = record_trace(teacher, schemas, r["message"], r["category"], r["expect"], random_today(rng),
                                 history=history_for(r["category"], rng, r["message"]))
            ok, why = trace_is_clean(trace, schemas)
            target = out if ok else rej
            payload = trace.__dict__ | ({} if ok else {"rejected": why})
            target.write(json.dumps(payload, ensure_ascii=False) + "\n")
            target.flush()
            kept += ok
            dropped += not ok
            calls = " -> ".join(c["name"] for c in trace.calls) or "(no call)"
            print(f"[{i}/{len(rows)}] {'ok  ' if ok else 'DROP'} {r['category']:<22} {calls:<45} {r['message'][:50]}")
    print(f"kept {kept}, dropped {dropped} -> {TRACES} / {REJECTED}")


def cmd_build(args):
    traces = [Trace(**{k: v for k, v in r.items() if k != "rejected"}) for r in _read_jsonl(TRACES)]
    train, valid = split_traces(traces, seed=args.seed)
    out = FT_DIR / f"data-{args.name}"
    counts = write_mlx_dataset(out, train, valid, _schemas())
    print(f"{out}: traces train={len(train)} valid={len(valid)} -> rows {counts}")
    rows = [json.loads(line) for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    lengths = sorted(row_token_lengths(rows, args.tokenizer))
    longest = lengths[-1] if lengths else 0
    p95 = lengths[int(0.95 * (len(lengths) - 1))] if lengths else 0
    print(f"row tokens: max={longest} p95={p95} median={lengths[len(lengths)//2] if lengths else 0}")
    if longest >= args.max_seq_length:
        print(f"WARNING: {sum(x >= args.max_seq_length for x in lengths)} row(s) reach --max-seq-length "
              f"{args.max_seq_length}; mlx-lm truncates the END of a row, which is the assistant turn being "
              f"trained. Train with --max-seq-length {((longest // 512) + 1) * 512} or higher.")


def cmd_train(args):
    adapter = FT_DIR / "adapters" / args.name
    cmd = [sys.executable, "-m", "mlx_lm.lora", "--model", args.model, "--train", "--fine-tune-type", "lora",
           "--data", str(FT_DIR / f"data-{args.data}"), "--adapter-path", str(adapter), "--iters", str(args.iters),
           "--batch-size", str(args.batch_size), "--num-layers", str(args.num_layers), "--learning-rate", str(args.lr),
           "--max-seq-length", str(args.max_seq_length), "--mask-prompt", "--steps-per-eval", "50",
           "--steps-per-report", "20", "--save-every", "100", "--seed", "7"]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)
    print(f"adapter -> {adapter}")


def cmd_eval(args):
    schemas = _schemas()
    suite = load_suite()
    if args.limit:
        suite = suite[: args.limit]
    results = []
    system_fn = SYSTEM_PROMPTS[args.prompt]
    tag = "" if args.prompt == "specialist" else f", {args.prompt} prompt"
    if args.teacher:
        results.append(evaluate(_teacher(), schemas, suite, f"teacher Sonnet 5{tag}", system_fn))
        print(results[-1].row())
    from companion.local_tools import LocalToolLLM

    for model in args.zero_shot or []:
        results.append(evaluate(LocalToolLLM(model), schemas, suite, f"zero-shot ({model.split('/')[-1]}){tag}", system_fn))
        print(results[-1].row())
    for spec in args.adapter or []:
        name, model = spec.split(":", 1)
        llm = LocalToolLLM(model, adapter_path=str(FT_DIR / "adapters" / name))
        results.append(evaluate(llm, schemas, suite, f"LoRA {name} ({model.split('/')[-1]}){tag}", system_fn))
        print(results[-1].row())
    FT_DIR.mkdir(parents=True, exist_ok=True)
    out = FT_DIR / (args.out or "eval.json")
    existing = json.loads(out.read_text()) if out.exists() and args.merge else []
    out.write_text(json.dumps(existing + [r.__dict__ for r in results], indent=2), encoding="utf-8")
    print("\n" + render_report(results) + f"\n\n(details incl. misses -> {out})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--per-category", type=int, default=40)
    g.add_argument("--categories", default="")
    g.add_argument("--append", action="store_true")
    g.set_defaults(fn=cmd_gen)
    t = sub.add_parser("trace")
    t.add_argument("--limit", type=int)
    t.add_argument("--categories", default="")
    t.add_argument("--seed", type=int, default=7)
    t.set_defaults(fn=cmd_trace)
    b = sub.add_parser("build")
    b.add_argument("--name", default="full")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--tokenizer", default=STUDENT_REPO, help="tokenizer used to measure row lengths")
    b.add_argument("--max-seq-length", type=int, default=4096, help="the value you intend to train with")
    b.set_defaults(fn=cmd_build)
    tr = sub.add_parser("train")
    tr.add_argument("--name", required=True)
    tr.add_argument("--model", required=True)
    tr.add_argument("--data", default="full")
    tr.add_argument("--iters", type=int, default=400)
    tr.add_argument("--batch-size", type=int, default=2)
    tr.add_argument("--num-layers", type=int, default=8)
    tr.add_argument("--lr", type=float, default=1e-4)
    tr.add_argument("--max-seq-length", type=int, default=4096)
    tr.set_defaults(fn=cmd_train)
    e = sub.add_parser("eval")
    e.add_argument("--teacher", action="store_true")
    e.add_argument("--zero-shot", action="append")
    e.add_argument("--adapter", action="append")
    e.add_argument("--limit", type=int)
    e.add_argument("--prompt", choices=sorted(SYSTEM_PROMPTS), default="specialist",
                   help="specialist = what the student trains on; production = what the app sends today")
    e.add_argument("--out")
    e.add_argument("--merge", action="store_true", help="append to an existing eval file instead of overwriting")
    e.set_defaults(fn=cmd_eval)
    args = p.parse_args()
    configure_logging()
    args.fn(args)


if __name__ == "__main__":
    main()
