"""Fine-tune the router classifier (see companion/router_ft.py for the why).

    python3 scripts/router_ft.py gen   [--per-category 45]           # Claude -> data/router_ft/synthetic.jsonl
    python3 scripts/router_ft.py build [--limit N] [--name full]      # -> data/router_ft/data-<name>/{train,valid,test}.jsonl
    python3 scripts/router_ft.py train --name qwen1.5b-full --model mlx-community/Qwen2.5-1.5B-Instruct-4bit --data full --iters 600
    python3 scripts/router_ft.py eval  --baseline --adapter qwen1.5b-full:mlx-community/Qwen2.5-1.5B-Instruct-4bit [...]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.logging_setup import configure_logging
from companion.paths import PROJECT_ROOT
from companion.router_ft import (
    FT_DIR,
    CompactPromptClassifier,
    Example,
    FewShotBaselineClassifier,
    evaluate,
    generate_synthetic,
    load_testset,
    render_report,
    split,
    write_mlx_dataset,
)

SYNTH = FT_DIR / "synthetic.jsonl"


def cmd_gen(args):
    from anthropic import Anthropic

    from companion.config import require_api_key
    from companion.llm import AnthropicLLM

    llm = AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=8000)
    cats = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None
    rows = generate_synthetic(llm, args.per_category, load_testset(), categories=cats)
    FT_DIR.mkdir(parents=True, exist_ok=True)
    out = FT_DIR / args.out
    with out.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e.__dict__) + "\n")
    print(f"wrote {len(rows)} examples -> {out}")


def _load_synth(names: list[str]) -> list[Example]:
    rows: list[Example] = []
    for name in names:
        path = FT_DIR / name
        rows += [Example(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def cmd_build(args):
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    train, valid = split(_load_synth(sources), limit=args.limit, cap_per_category=args.cap_per_category)
    out = FT_DIR / f"data-{args.name}"
    write_mlx_dataset(out, train, valid, load_testset())
    print(f"{out}: train={len(train)} valid={len(valid)} test={len(load_testset())}")


def cmd_train(args):
    adapter = FT_DIR / "adapters" / args.name
    cmd = [sys.executable, "-m", "mlx_lm.lora", "--model", args.model, "--train", "--fine-tune-type", "lora",
           "--data", str(FT_DIR / f"data-{args.data}"), "--adapter-path", str(adapter), "--iters", str(args.iters),
           "--batch-size", str(args.batch_size), "--num-layers", str(args.num_layers), "--learning-rate", str(args.lr),
           "--max-seq-length", "512", "--mask-prompt", "--steps-per-eval", "100", "--steps-per-report", "50",
           "--save-every", "200", "--seed", "7"]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)
    print(f"adapter -> {adapter}")


def cmd_eval(args):
    from companion.llm import LocalLLM

    testset = load_testset()
    results = []
    if args.baseline:
        from companion.default_tools import default_tool_registry
        from companion.router import CLASSIFIER_MODEL

        tools = "\n".join(f"- {t.name}: {t.description}" for t in default_tool_registry())
        base = LocalLLM(repo=CLASSIFIER_MODEL, max_tokens=120)
        results.append(evaluate(FewShotBaselineClassifier(f"baseline few-shot ({CLASSIFIER_MODEL.split('/')[-1]})", base, tools), testset))
        print(results[-1].row())
    for spec in args.adapter or []:
        name, model = spec.split(":", 1)
        adapter = FT_DIR / "adapters" / name
        llm = LocalLLM(repo=model, max_tokens=40, adapter_path=str(adapter))
        results.append(evaluate(CompactPromptClassifier(f"LoRA {name} ({model.split('/')[-1]})", llm), testset))
        print(results[-1].row())
    for model in args.zero_shot or []:
        llm = LocalLLM(repo=model, max_tokens=40)
        results.append(evaluate(CompactPromptClassifier(f"zero-shot compact ({model.split('/')[-1]})", llm), testset))
        print(results[-1].row())
    report = render_report(results)
    out = FT_DIR / "eval.json"
    out.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    print("\n" + report + f"\n\n(details incl. misses -> {out})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--per-category", type=int, default=45)
    g.add_argument("--categories", default="", help="comma-separated subset of CATEGORIES (default: all)")
    g.add_argument("--out", default="synthetic.jsonl", help="output file name under data/router_ft/")
    g.set_defaults(fn=cmd_gen)
    b = sub.add_parser("build")
    b.add_argument("--limit", type=int)
    b.add_argument("--cap-per-category", type=int, help="class-balance ablation: keep at most N per category")
    b.add_argument("--sources", default="synthetic.jsonl", help="comma-separated jsonl files under data/router_ft/")
    b.add_argument("--name", default="full")
    b.set_defaults(fn=cmd_build)
    t = sub.add_parser("train")
    t.add_argument("--name", required=True)
    t.add_argument("--model", required=True)
    t.add_argument("--data", default="full")
    t.add_argument("--iters", type=int, default=600)
    t.add_argument("--batch-size", type=int, default=4)
    t.add_argument("--num-layers", type=int, default=8)
    t.add_argument("--lr", type=float, default=1e-4)
    t.set_defaults(fn=cmd_train)
    e = sub.add_parser("eval")
    e.add_argument("--baseline", action="store_true")
    e.add_argument("--adapter", action="append")
    e.add_argument("--zero-shot", action="append")
    e.set_defaults(fn=cmd_eval)
    args = p.parse_args()
    configure_logging()
    args.fn(args)


if __name__ == "__main__":
    main()
