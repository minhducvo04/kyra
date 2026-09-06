"""Fine-tune a small local model to be the TurnRouter's classifier.

Why: the router's classifier today is Llama-3.2-3B driven by a ~900-token
few-shot prompt (router.CLASSIFIER_PROMPT), scoring 96.6% on a 29-case suite
at ~0.36 s per turn. That prompt is paid on every single turn. A LoRA
adapter on a 0.5B-1.5B model trained on the same decision should reach the
same accuracy from a ~60-token prompt, faster - and it is a real, measured
post-training project rather than a claim.

Pipeline (scripts/router_ft.py drives it):
  gen   -> synthetic labeled messages from Claude, per category, deduped,
           and scrubbed of anything that appears in the held-out test set
  build -> {train,valid}.jsonl in mlx_lm's "messages" chat format
           (+ the handwritten test set copied as test.jsonl)
  train -> `python -m mlx_lm.lora` with a LoRA adapter per run
  eval  -> baseline few-shot 3B vs each adapter on the SAME held-out set:
           accuracy, path-only accuracy, mean latency, prompt tokens

The held-out test set (tests/data/router_testset.jsonl) is handwritten
and never generated, never trained on, and never used to pick a checkpoint.
That is the whole point of it.
"""
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from companion.llm import LLMBackend, Message
from companion.paths import DATA_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

FT_DIR = DATA_DIR / "router_ft"
TESTSET_PATH = PROJECT_ROOT / "tests" / "data" / "router_testset.jsonl"

# The prompt the fine-tuned model sees. Short on purpose - the few-shot
# examples and tool descriptions that make router.CLASSIFIER_PROMPT long
# are what the adapter is meant to have learned.
COMPACT_SYSTEM = (
    "Classify the user's message for an assistant router. Reply with JSON only: "
    '{"path": "tool"|"text", "backend": "claude"|"local"}. '
    "tool = it asks for a reminder, tech news, science facts, saving/reviewing learning items, "
    "job-application tracking/drafting/autofill, or saving a durable fact about the user. "
    "text = anything else; backend claude for explanations, careful reasoning, or work coordination, local for casual chat."
)

# category id -> (path, backend, description for the generator, seed phrasings)
CATEGORIES: dict[str, tuple[str, str, str, list[str]]] = {
    "add_reminder": ("tool", "claude", "asking to be reminded to do something, or not to forget something, often with a time",
                     ["remind me to call mom tomorrow", "don't let me forget the rent on the 1st"]),
    "list_reminders": ("tool", "claude", "asking what reminders/tasks are outstanding",
                       ["what's on my list", "what do I still have to do"]),
    "complete_reminder": ("tool", "claude", "saying a reminder/task is done and should be closed",
                          ["mark the milk one as done", "I paid the bill, close that reminder"]),
    "snooze_reminder": ("tool", "claude", "asking to push a reminder to a later date/time",
                        ["push that reminder to next week", "move the dentist reminder to Friday"]),
    "tech_news": ("tool", "claude", "asking to fetch today's tech headlines/news",
                  ["what's happening in tech today", "any tech news"]),
    "science_facts": ("tool", "claude", "asking to fetch science news/headlines/facts from feeds",
                      ["what's new in science", "give me a science fact from the news"]),
    "save_learning_item": ("tool", "claude", "asking to save something just learned for later spaced-repetition review",
                           ["save that summary so I can review it later", "add this to my review queue"]),
    "due_learning_reviews": ("tool", "claude", "asking what learning items are due for review",
                             ["what do I need to review today", "anything due for review"]),
    "mark_learning_reviewed": ("tool", "claude", "reporting the outcome of a review (remembered or forgot) so it gets rescheduled",
                               ["yeah I remembered that one", "I forgot the Raft one, reset it"]),
    "add_job_application": ("tool", "claude", "asking to log/track a job application at a company for a role",
                            ["I applied to Stripe for a backend role, log it", "track my application to Cursor"]),
    "list_job_applications": ("tool", "claude", "asking which job applications are being tracked or their statuses",
                              ["what jobs am I tracking", "which applications are still open"]),
    "update_job_application_status": ("tool", "claude", "reporting a status change on a tracked application (interviewing/offer/rejected/withdrawn)",
                                      ["mark the Stripe application as interviewing", "Cursor rejected me, update it"]),
    "draft_application_material": ("tool", "claude", "asking to draft a cover letter or resume bullets for a job",
                                   ["draft a cover letter for this posting", "write me resume bullets for an ML role"]),
    "autofill_job_application": ("tool", "claude", "asking to fill in a job application form at a URL (Greenhouse)",
                                 ["fill out this application for me: greenhouse.io/acme/jobs/123", "autofill that posting I sent"]),
    "save_memory_note": ("tool", "claude", "asking to remember/note a durable fact about the user (preference, person, ongoing project)",
                         ["remember that I prefer standing desks", "note that my sister's name is Linh"]),
    "text_explain": ("text", "claude", "asking for an explanation or summary of a technical topic in some depth (not asking to save it)",
                     ["give me a quick summary of the CAP theorem", "explain how Raft elects a leader"]),
    "text_reasoning": ("text", "claude", "asking for help thinking through a design decision, tradeoff, or plan",
                       ["help me decide between SQLite and Postgres here", "review my sharding approach"]),
    "text_coordination": ("text", "claude", "asking to coordinate or hand work to Claude Code / another agent",
                          ["can you coordinate this with Claude Code", "hand this refactor to Claude Code"]),
    "text_casual": ("text", "local", "casual chat, small talk, feelings, jokes, greetings",
                    ["how's it going", "ugh long day"]),
    "text_casual_science": ("text", "local", "conversational curiosity about a science topic, NOT asking to fetch news",
                            ["tell me something cool about black holes", "why is the sky blue"]),
    "text_trivia": ("text", "local", "quick low-stakes questions, simple arithmetic, opinions",
                    ["what's 15% of 80", "what's your favorite season"]),
    "text_hard_negative": ("text", "local", "messages that CONTAIN a tool keyword (remind, news, save, note, list, apply, fill, snooze, review, job) "
                           "but are casual conversation, not a request for that tool; label backend local unless it is a real explanation request",
                          ["remember when we talked about black holes? fun", "what's the news with your day", "I applied a patch yesterday"]),
}

GEN_PROMPT = """Generate {n} distinct, realistic messages a user might type or say to a personal AI assistant, all of which fall \
into exactly this category:

Category: {cat}
Meaning: {desc}
Seed examples (do NOT copy these, vary them widely): {seeds}

Vary length (2 to 25 words), tone (casual, terse, polite, rushed), phrasing, and specifics (real-sounding names, \
companies, dates, topics). Include a few with typos or missing punctuation. About 1 in 10 may mix in a Vietnamese \
word or two the way a bilingual speaker does. No numbering, no commentary.

Output a JSON array of strings only."""


@dataclass
class Example:
    message: str
    path: str
    backend: str
    category: str


def _norm(msg: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", msg.lower()).strip()


def parse_json_array(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def load_testset(path: Path = TESTSET_PATH) -> list[Example]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [Example(r["message"], r["path"], r["backend"], r.get("note", "")) for r in rows]


def generate_synthetic(llm: LLMBackend, per_category: int, testset: list[Example]) -> list[Example]:
    """One Claude call per category; dedupes across categories and drops
    any message that matches the held-out test set (leakage guard)."""
    blocked = {_norm(e.message) for e in testset}
    seen: set[str] = set()
    out: list[Example] = []
    for cat, (path, backend, desc, seeds) in CATEGORIES.items():
        raw = llm.respond(
            system="You write realistic user messages for training a small classifier. JSON array of strings only.",
            history=[],
            user_input=GEN_PROMPT.format(n=per_category, cat=cat, desc=desc, seeds=json.dumps(seeds)),
        )
        msgs = parse_json_array(raw)
        kept = 0
        for msg in msgs:
            key = _norm(msg)
            if not key or key in seen or key in blocked:
                continue
            seen.add(key)
            out.append(Example(msg, path, backend, cat))
            kept += 1
        logger.info("gen %-32s asked=%d got=%d kept=%d", cat, per_category, len(msgs), kept)
    return out


def split(examples: list[Example], valid_frac: float = 0.1, seed: int = 7, limit: int | None = None) -> tuple[list[Example], list[Example]]:
    """Deterministic shuffle + split. `limit` caps the TRAIN size (for the
    data-size ablation) after the split, so valid stays the same."""
    rng = random.Random(seed)
    rows = list(examples)
    rng.shuffle(rows)
    n_valid = max(1, int(len(rows) * valid_frac))
    valid, train = rows[:n_valid], rows[n_valid:]
    if limit is not None:
        train = train[:limit]
    return train, valid


def to_chat_row(e: Example, system: str = COMPACT_SYSTEM) -> dict:
    return {"messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": e.message},
        {"role": "assistant", "content": json.dumps({"path": e.path, "backend": e.backend})},
    ]}


def write_mlx_dataset(out_dir: Path, train: list[Example], valid: list[Example], test: list[Example]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("valid", valid), ("test", test)):
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps(to_chat_row(e)) + "\n")


def parse_label(text: str) -> tuple[str, str]:
    """Robustly read {"path","backend"} out of a model reply; anything
    unreadable counts as text/claude (the router's own safe default)."""
    m = re.search(r"\{.*?\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            path = d.get("path") if d.get("path") in ("text", "tool") else "text"
            backend = d.get("backend") if d.get("backend") in ("claude", "local") else "claude"
            if path == "tool":
                backend = "claude"
            return path, backend
        except json.JSONDecodeError:
            pass
    return "text", "claude"


@dataclass
class Prediction:
    message: str
    gold_path: str
    gold_backend: str
    path: str
    backend: str
    latency_s: float
    prompt_tokens: int
    category: str = ""

    @property
    def correct(self) -> bool:
        return self.path == self.gold_path and (self.path == "tool" or self.backend == self.gold_backend)

    @property
    def path_correct(self) -> bool:
        return self.path == self.gold_path


@dataclass
class EvalResult:
    system: str
    n: int
    accuracy: float
    path_accuracy: float
    mean_latency_s: float
    p90_latency_s: float
    mean_prompt_tokens: float
    misses: list[dict]

    def row(self) -> str:
        return (f"| {self.system} | {self.accuracy*100:.1f}% | {self.path_accuracy*100:.1f}% | "
                f"{self.mean_latency_s:.2f}s | {self.p90_latency_s:.2f}s | {self.mean_prompt_tokens:.0f} |")


def score(system: str, preds: list[Prediction]) -> EvalResult:
    n = len(preds)
    lat = sorted(p.latency_s for p in preds)
    p90 = lat[min(n - 1, int(0.9 * n))] if n else 0.0
    return EvalResult(
        system=system, n=n,
        accuracy=sum(p.correct for p in preds) / n if n else 0.0,
        path_accuracy=sum(p.path_correct for p in preds) / n if n else 0.0,
        mean_latency_s=sum(lat) / n if n else 0.0, p90_latency_s=p90,
        mean_prompt_tokens=sum(p.prompt_tokens for p in preds) / n if n else 0.0,
        misses=[asdict(p) for p in preds if not p.correct],
    )


class Classifier:
    """Anything that maps a message to (path, backend) and can report its
    prompt cost - the baseline few-shot router and the adapters both fit."""

    name: str

    def predict(self, message: str) -> tuple[str, str, int]:
        raise NotImplementedError


class CompactPromptClassifier(Classifier):
    """A LocalLLM (base or with a LoRA adapter) driven by COMPACT_SYSTEM."""

    def __init__(self, name: str, llm, system: str = COMPACT_SYSTEM):
        self.name, self._llm, self._system = name, llm, system

    def predict(self, message: str) -> tuple[str, str, int]:
        raw = self._llm.respond(system=self._system, history=[], user_input=message)
        path, backend = parse_label(raw)
        return path, backend, self._llm.prompt_token_count(self._system, message)


class FewShotBaselineClassifier(Classifier):
    """The router as shipped: router.CLASSIFIER_PROMPT (few-shot + tool
    descriptions) on the 3B model. Reuses the router's own prompt and
    parser so the baseline is exactly what production runs."""

    def __init__(self, name: str, llm, tool_descriptions: str):
        from companion.router import CLASSIFIER_PROMPT

        self.name, self._llm = name, llm
        self._template = CLASSIFIER_PROMPT
        self._tools = tool_descriptions

    def predict(self, message: str) -> tuple[str, str, int]:
        from companion.router import _parse_json

        prompt = self._template.format(tools=self._tools, message=message)
        raw = self._llm.respond(system="", history=[], user_input=prompt)
        parsed = _parse_json(raw) or {}
        path = parsed.get("path") if parsed.get("path") in ("text", "tool") else "text"
        backend = parsed.get("backend") if parsed.get("backend") in ("claude", "local") else "claude"
        if path == "tool":
            backend = "claude"
        return path, backend, self._llm.prompt_token_count("", prompt)


def evaluate(clf: Classifier, testset: list[Example]) -> EvalResult:
    preds = []
    for e in testset:
        t0 = time.perf_counter()
        path, backend, ptoks = clf.predict(e.message)
        preds.append(Prediction(e.message, e.path, e.backend, path, backend, time.perf_counter() - t0, ptoks, e.category))
    return score(clf.name, preds)


def render_report(results: list[EvalResult], notes: str = "") -> str:
    lines = ["| System | Accuracy | Path-only | Mean latency | p90 | Prompt tokens |", "|---|---|---|---|---|---|"]
    lines += [r.row() for r in results]
    out = "\n".join(lines)
    if notes:
        out += "\n\n" + notes
    return out


__all__ = [
    "COMPACT_SYSTEM", "CATEGORIES", "Example", "EvalResult", "Prediction", "Classifier", "CompactPromptClassifier",
    "FewShotBaselineClassifier", "FT_DIR", "TESTSET_PATH", "Message", "evaluate", "generate_synthetic",
    "load_testset", "parse_json_array", "parse_label", "render_report", "score", "split", "to_chat_row",
    "write_mlx_dataset",
]
