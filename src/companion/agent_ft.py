"""Distill the tool-calling "Agent Specialist" into a local model - the
harness, the held-out suite loader, and the teacher-trace pipeline.

Why: every `tool` turn today is a Claude call carrying ~7K tokens of tool
schemas. docs/router-model-benchmark.md found Qwen2.5-7B flawless on an
11-case tool suite but called it too small to act on. This module is the
harder, handwritten suite (tests/data/tool_testset.jsonl) plus the pipeline
to train a local student on Claude's own traces and measure it against
Claude on that suite. Same discipline as router_ft.py: the suite is
written by hand first, never generated, never trained on, never used to
pick a checkpoint; the generator drops any message that normalizes to a
suite message.

What is scored per case: the *sequence* of tool calls (names, in order
unless the case says any order), every expected argument (exact value or
a matcher: icontains / date / regex / startswith / any), and that a
non-empty reply came back. A no-tool case is correct only if nothing was
called - over-triggering on keyword bait is the failure mode this whole
router has guarded against from the start.

Training rows: mlx-lm's `--mask-prompt` trains only the last message, so
a multi-step trace (list -> act -> reply) is expanded into one row per
assistant turn. Each row carries the `tools` list the chat template renders.
"""
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from companion.llm import LLMBackend, Message
from companion.local_tools import ToolCall, to_openai_tools
from companion.paths import DATA_DIR, PROJECT_ROOT
from companion.router_ft import CATEGORIES as ROUTER_CATEGORIES
from companion.router_ft import GEN_PROMPT, parse_json_array

logger = logging.getLogger(__name__)

FT_DIR = DATA_DIR / "tool_ft"
TESTSET_PATH = PROJECT_ROOT / "tests" / "data" / "tool_testset.jsonl"
STUDENT_REPO = "mlx-community/Qwen2.5-7B-Instruct-4bit"
FIXED_TODAY = "2026-09-06T10:00:00-07:00"  # a Sunday; every dated expectation in the suite is relative to this

SPECIALIST_SYSTEM = (
    "You are Kyra's tool specialist. Today is {weekday} {today}. "
    "When the user's message asks for something a tool does, call the tool. If an action needs an id you don't "
    "have (a reminder, an application, a review item referred to by description), call the listing tool first, "
    "then act on the matching id. Never invent ids, dates, or facts. When the essential detail is missing (what "
    "to remind, which company), ask instead of calling. If nothing matches a tool, answer in one or two friendly "
    "sentences without calling anything. After tools run, reply briefly."
)


def specialist_system(today: str = FIXED_TODAY) -> str:
    dt = datetime.fromisoformat(today)
    return SPECIALIST_SYSTEM.format(weekday=dt.strftime("%A"), today=dt.strftime("%Y-%m-%d %H:%M"))


# What ConversationManager._build_system() actually sends on a tool turn: persona,
# date, durable notes, retrieved memories - and nothing tool-specific at all. A
# student trained on SPECIALIST_SYSTEM sees a different prompt in production, so the
# eval measures both rather than assuming the adapter transfers. Fixed sample notes
# and memories keep the measurement reproducible.
SAMPLE_NOTES = """## background
- (2026-08-30) Duc is building Kyra as a hands-on AI-pipeline learning project.

## career
- (2026-09-01) Duc is targeting AI engineer roles for after graduation.

## preferences
- (2026-09-03) Duc prefers short replies unless he asks for detail."""

SAMPLE_MEMORIES = [
    "Duc said: can you explain how Raft elects a leader",
    "Kyra replied: Followers wait a randomized timeout, then campaign for votes.",
    "Duc said: I have an interview coming up",
]


def production_system(today: str = FIXED_TODAY) -> str:
    """The prompt shape production sends today, for measuring prompt shift."""
    from companion.persona import KYRA

    dt = datetime.fromisoformat(today)
    now_str = dt.strftime("%A, %B %-d, %Y, %-I:%M %p")
    memory_block = "\n".join(f"- {m}" for m in SAMPLE_MEMORIES)
    return (
        f"{KYRA.system_prompt()}\n\n"
        f"Current date and time: {now_str}\n\n"
        f"Durable facts you've saved about Duc (always shown, not search-retrieved):\n{SAMPLE_NOTES}\n\n"
        f"Relevant things you remember about Duc from past conversations:\n{memory_block}"
    )


SYSTEM_PROMPTS = {"specialist": specialist_system, "production": production_system}


# ----------------------------------------------------------------------------- suite

@dataclass
class ToolCase:
    message: str
    expected_calls: list[dict]  # [{"name": ..., "args": {key: value-or-matcher}}]
    tool_results: dict = field(default_factory=dict)  # tool name -> result, or list of results per call
    history: list[dict] = field(default_factory=list)
    any_order: bool = False
    note: str = ""
    today: str = FIXED_TODAY

    @property
    def category(self) -> str:
        if not self.expected_calls:
            return "no_tool"
        return "multi" if len(self.expected_calls) > 1 else self.expected_calls[0]["name"]


def load_suite(path: Path = TESTSET_PATH) -> list[ToolCase]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [ToolCase(**r) for r in rows]


# ----------------------------------------------------------------------------- fake registry

def default_listings(today: str) -> dict[str, dict]:
    """What the listing tools return when a case doesn't script them. Rich
    on purpose: the pilot showed that with empty lists every find-then-act
    trace ended at "I don't see that reminder", so the student would have
    learned list -> apologize instead of list -> act. Dates are relative to
    `today` so "push it to Friday" stays a real date computation."""
    t = datetime.fromisoformat(today)
    d = lambda days, hour=9: (t + timedelta(days=days)).replace(hour=hour, minute=0, second=0).isoformat()  # noqa: E731
    return {
        "list_reminders": {"reminders": [
            {"id": 2, "text": "pay electricity bill", "due_at": d(2), "created_at": d(-5), "done": False},
            {"id": 3, "text": "book dentist appointment", "due_at": d(4), "created_at": d(-4), "done": False},
            {"id": 5, "text": "submit taxes", "due_at": d(6), "created_at": d(-3), "done": False},
            {"id": 6, "text": "call mom", "due_at": d(1, 18), "created_at": d(-2), "done": False},
            {"id": 8, "text": "renew passport photo", "due_at": d(9), "created_at": d(-2), "done": False},
            {"id": 9, "text": "gym", "due_at": d(0, 18), "created_at": d(-1), "done": False},
            {"id": 11, "text": "return library books", "due_at": d(3), "created_at": d(-1), "done": False},
            {"id": 12, "text": "send thank-you email to the recruiter", "due_at": d(1), "created_at": d(0), "done": False},
        ]},
        "list_job_applications": {"applications": [
            {"id": 7, "company": "Stripe", "role": "Backend Engineer", "status": "applied", "link": None, "notes": None},
            {"id": 8, "company": "Cursor", "role": "AI Engineer", "status": "applied", "link": None, "notes": None},
            {"id": 10, "company": "Meta", "role": "Software Engineer", "status": "interviewing", "link": None, "notes": None},
            {"id": 13, "company": "Slack", "role": "Platform Engineer", "status": "applied", "link": None, "notes": None},
            {"id": 14, "company": "Anthropic", "role": "Research Engineer", "status": "applied", "link": None, "notes": None},
            {"id": 15, "company": "Epsilon", "role": "AI Engineer", "status": "interviewing", "link": None, "notes": None},
        ]},
        "due_learning_reviews": {"due": [
            {"id": 4, "topic": "Raft leader election", "summary": "Followers time out and start an election; a majority elects a leader.", "key_takeaway": "Randomized timeouts avoid split votes.", "review_count": 1},
            {"id": 9, "topic": "TCP congestion control", "summary": "Slow start, congestion avoidance, fast retransmit.", "key_takeaway": "AIMD keeps flows fair.", "review_count": 2},
            {"id": 16, "topic": "CAP theorem", "summary": "Under a partition a system must choose consistency or availability.", "key_takeaway": "Partitions force the C-vs-A choice.", "review_count": 1},
            {"id": 17, "topic": "Dockerfile syntax", "summary": "FROM, RUN, COPY, CMD; layers are cached in order.", "key_takeaway": "Order instructions by change frequency.", "review_count": 3},
            {"id": 21, "topic": "SQL joins", "summary": "Inner keeps matches; left keeps all left rows; outer keeps both.", "key_takeaway": "Draw the Venn diagram.", "review_count": 1},
            {"id": 22, "topic": "Bayes theorem", "summary": "Posterior is proportional to likelihood times prior.", "key_takeaway": "Update beliefs with evidence.", "review_count": 2},
        ]},
    }


def _default_result(name: str, kwargs: dict, today: str) -> dict:
    nxt = (datetime.fromisoformat(today) + timedelta(days=1)).isoformat()
    if name in ("list_reminders", "list_job_applications", "due_learning_reviews"):
        return default_listings(today)[name]
    table = {
        "add_reminder": {"id": 101, "text": kwargs.get("text"), "due_at": kwargs.get("due_at"), "done": False},
        "complete_reminder": {"ok": True},
        "snooze_reminder": {"ok": True},
        "save_learning_item": {"id": 31, "topic": kwargs.get("topic"), "next_review_at": nxt},
        "mark_learning_reviewed": {"review_count": 2, "next_review_at": nxt, "streak_days": 3},
        "add_job_application": {"id": 12, "company": kwargs.get("company"), "role": kwargs.get("role"), "status": "applied",
                                "link": kwargs.get("link"), "notes": kwargs.get("notes")},
        "update_job_application_status": {"id": kwargs.get("id"), "status": kwargs.get("status")},
        "draft_application_material": {"material_type": kwargs.get("material_type"), "draft": "(draft text omitted in the harness)"},
        "autofill_job_application": {"filled": 9, "skipped": 2, "submitted": False, "summary_path": "data/job_autofill_logs/run.md"},
        "save_memory_note": {"saved": True, "category": kwargs.get("category"), "note": kwargs.get("note")},
        "tech_news": {"headlines": [{"title": "Example headline about a new chip", "source": "Ars Technica", "link": "https://example.com/a"}], "errors": []},
        "science_facts": {"headlines": [{"title": "Example finding about coral reefs", "source": "Science Daily", "link": "https://example.com/s"}], "errors": []},
    }
    return table.get(name, {"ok": True})


class FakeRegistry:
    """Looks like companion.tools.ToolRegistry to a tool loop: real
    schemas, scripted results, and it records every call. Missing required
    or unknown arguments raise TypeError, exactly as `tool.run(**kwargs)`
    would on the real registry, so the model sees the same error path."""

    def __init__(self, schemas: list[dict], results: dict | None = None, today: str = FIXED_TODAY):
        self._schemas = schemas
        self._by_name = {s["name"]: s for s in schemas}
        self._results = {k: (list(v) if isinstance(v, list) else v) for k, v in (results or {}).items()}
        self._today = today
        self.calls: list[ToolCall] = []
        # what each call actually returned, or the error it raised - so a recorded
        # trace can never claim a call succeeded when the registry rejected it.
        self.results: list[dict] = []

    def schemas(self) -> list[dict]:
        return self._schemas

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def run(self, name: str, **kwargs):
        if name not in self._by_name:
            raise KeyError(f"no such tool: {name!r} (have: {sorted(self._by_name)})")
        schema = self._by_name[name].get("input_schema", {})
        missing = [k for k in schema.get("required", []) if k not in kwargs]
        unknown = [k for k in kwargs if k not in schema.get("properties", {})]
        self.calls.append(ToolCall(name, dict(kwargs)))
        if missing or unknown:
            err = TypeError(f"{name}() missing required {missing} / unexpected {unknown}")
            self.results.append({"error": str(err)})
            raise err
        scripted = self._results.get(name)
        if isinstance(scripted, list):
            result = scripted.pop(0) if scripted else _default_result(name, kwargs, self._today)
        elif scripted is not None:
            result = scripted
        else:
            result = _default_result(name, kwargs, self._today)
        self.results.append(result)
        return result


# ----------------------------------------------------------------------------- scoring

def match_value(expected, actual) -> bool:
    if isinstance(expected, dict):
        if expected.get("any"):
            return actual is not None
        if "icontains" in expected:
            return isinstance(actual, str) and expected["icontains"].lower() in actual.lower()
        if "startswith" in expected:
            return isinstance(actual, str) and actual.startswith(expected["startswith"])
        if "date" in expected:  # ISO datetime whose calendar date matches
            return isinstance(actual, str) and actual[:10] == expected["date"]
        if "regex" in expected:
            return isinstance(actual, str) and re.search(expected["regex"], actual) is not None
        if "in" in expected:
            return actual in expected["in"]
        return expected == actual
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


def match_args(expected: dict, actual: dict) -> bool:
    return all(k in actual and match_value(v, actual[k]) for k, v in expected.items())


def _call_matches(expected: dict, call: ToolCall) -> bool:
    return call.name == expected["name"] and match_args(expected.get("args", {}), call.args)


@dataclass
class CaseResult:
    message: str
    category: str
    expected: list[dict]
    actual: list[dict]
    reply: str
    latency_s: float
    sequence_ok: bool
    args_ok: bool

    @property
    def reply_ok(self) -> bool:
        return bool(self.reply.strip())

    @property
    def correct(self) -> bool:
        return self.sequence_ok and self.args_ok and self.reply_ok

    @property
    def over_trigger(self) -> bool:
        return not self.expected and bool(self.actual)

    @property
    def under_trigger(self) -> bool:
        return bool(self.expected) and not self.actual


def score_case(case: ToolCase, calls: list[ToolCall], reply: str, latency_s: float = 0.0) -> CaseResult:
    exp = case.expected_calls
    names_ok = [c.name for c in calls] == [e["name"] for e in exp]
    if case.any_order and len(calls) == len(exp):
        remaining = list(calls)
        args_ok = True
        for e in exp:
            hit = next((c for c in remaining if _call_matches(e, c)), None)
            if hit is None:
                args_ok = False
                break
            remaining.remove(hit)
        names_ok = sorted(c.name for c in calls) == sorted(e["name"] for e in exp)
    else:
        args_ok = names_ok and all(_call_matches(e, c) for e, c in zip(exp, calls, strict=False))
    return CaseResult(
        message=case.message, category=case.category, expected=exp,
        actual=[{"name": c.name, "args": c.args} for c in calls], reply=reply, latency_s=latency_s,
        sequence_ok=names_ok, args_ok=args_ok,
    )


@dataclass
class SuiteResult:
    system: str
    n: int
    accuracy: float
    sequence_accuracy: float
    over_trigger_rate: float  # over the no-tool cases
    under_trigger_rate: float  # over the tool cases
    mean_latency_s: float
    p90_latency_s: float
    misses: list[dict]

    def row(self) -> str:
        return (f"| {self.system} | {self.accuracy*100:.1f}% | {self.sequence_accuracy*100:.1f}% | "
                f"{self.over_trigger_rate*100:.1f}% | {self.under_trigger_rate*100:.1f}% | "
                f"{self.mean_latency_s:.2f}s | {self.p90_latency_s:.2f}s |")


def summarize(system: str, results: list[CaseResult]) -> SuiteResult:
    n = len(results)
    no_tool = [r for r in results if not r.expected]
    tool = [r for r in results if r.expected]
    lat = sorted(r.latency_s for r in results)
    p90 = lat[min(n - 1, int(0.9 * n))] if n else 0.0
    misses = []
    for r in results:
        if not r.correct:
            d = asdict(r)
            d["why"] = "over-trigger" if r.over_trigger else "under-trigger" if r.under_trigger else \
                "wrong sequence" if not r.sequence_ok else "wrong args" if not r.args_ok else "empty reply"
            misses.append(d)
    return SuiteResult(
        system=system, n=n,
        accuracy=sum(r.correct for r in results) / n if n else 0.0,
        sequence_accuracy=sum(r.sequence_ok for r in results) / n if n else 0.0,
        over_trigger_rate=sum(r.over_trigger for r in no_tool) / len(no_tool) if no_tool else 0.0,
        under_trigger_rate=sum(r.under_trigger for r in tool) / len(tool) if tool else 0.0,
        mean_latency_s=sum(lat) / n if n else 0.0, p90_latency_s=p90, misses=misses,
    )


def run_case(backend, schemas: list[dict], case: ToolCase, system_fn=specialist_system) -> CaseResult:
    registry = FakeRegistry(schemas, case.tool_results, today=case.today)
    history = [Message(role=h["role"], content=h["content"]) for h in case.history]
    t0 = time.perf_counter()
    reply = backend.respond_with_tools(system_fn(case.today), history, case.message, registry)
    return score_case(case, registry.calls, reply, time.perf_counter() - t0)


def evaluate(backend, schemas: list[dict], suite: list[ToolCase], name: str, system_fn=specialist_system) -> SuiteResult:
    """`backend` is anything with respond_with_tools(system, history, user_input, registry)."""
    results = []
    for i, case in enumerate(suite, 1):
        r = run_case(backend, schemas, case, system_fn)
        results.append(r)
        logger.info("%s [%d/%d] %s %s", name, i, len(suite), "ok " if r.correct else "MISS", case.message[:60])
    return summarize(name, results)


def render_report(results: list[SuiteResult]) -> str:
    lines = ["| System | Accuracy | Sequence | Over-trigger | Under-trigger | Mean latency | p90 |",
             "|---|---|---|---|---|---|---|"]
    lines += [r.row() for r in results]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- training data

# category -> (expect, description for the generator, seeds). expect: "tool" = the teacher must call at
# least one tool; "none" = it must call nothing. Traces that contradict their category are dropped.
GEN_CATEGORIES: dict[str, tuple[str, str, list[str]]] = {
    **{cat: ("tool", desc, seeds) for cat, (path, _b, desc, seeds) in ROUTER_CATEGORIES.items() if path == "tool"},
    "find_then_act": ("tool",
                      "referring to an EXISTING reminder, tracked job application, or review item by description rather "
                      "than by id, and asking to complete / snooze / update / mark it - the assistant has to look it up first",
                      ["the dentist one is done", "push the rent reminder to Friday", "Stripe moved me to onsite, update it",
                       "I remembered the Raft one"]),
    "two_tools": ("tool",
                  "one message asking for two different things that are two different tools (log an application AND set a "
                  "follow-up reminder; save a note about someone AND remind me to thank them; my reminders AND my due reviews)",
                  ["log that I applied to Figma and remind me to follow up Thursday",
                   "note that Linh recommended the book and remind me to return it Sunday"]),
    "underspecified": ("none",
                       "a tool-shaped request with the essential detail missing, so the right move is to ask a short question "
                       "rather than call anything (remind me / log an application / save that / update the status - with no what/which)",
                       ["remind me", "log a job application", "update the status", "save that for later"]),
    "no_tool_casual": ("none", ROUTER_CATEGORIES["text_casual"][2], ROUTER_CATEGORIES["text_casual"][3]),
    "no_tool_curiosity": ("none", ROUTER_CATEGORIES["text_casual_science"][2] + "; or a request for an explanation of a technical topic",
                          ROUTER_CATEGORIES["text_casual_science"][3] + ROUTER_CATEGORIES["text_explain"][3]),
    "no_tool_bait_claude": ("none", ROUTER_CATEGORIES["text_hard_negative_claude"][2], ROUTER_CATEGORIES["text_hard_negative_claude"][3]),
    "no_tool_bait_local": ("none", ROUTER_CATEGORIES["text_hard_negative_local"][2], ROUTER_CATEGORIES["text_hard_negative_local"][3]),
}


def _norm(msg: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", msg.lower()).strip()


def generate_messages(
    llm: LLMBackend, per_category: int, blocked_messages: list[str], categories: list[str] | None = None,
) -> list[dict]:
    """One Claude call per category -> [{message, category, expect}], deduped
    and scrubbed against the held-out suite (leakage guard)."""
    blocked = {_norm(m) for m in blocked_messages}
    seen: set[str] = set()
    out: list[dict] = []
    for cat, (expect, desc, seeds) in GEN_CATEGORIES.items():
        if categories and cat not in categories:
            continue
        raw = llm.respond(
            system="You write realistic user messages for training a small assistant. JSON array of strings only.",
            history=[], user_input=GEN_PROMPT.format(n=per_category, cat=cat, desc=desc, seeds=json.dumps(seeds)),
        )
        kept = 0
        for msg in parse_json_array(raw):
            key = _norm(msg)
            if not key or key in seen or key in blocked:
                continue
            seen.add(key)
            out.append({"message": msg, "category": cat, "expect": expect})
            kept += 1
        logger.info("gen %-24s asked=%d kept=%d", cat, per_category, kept)
    return out


SEED_EXPLANATIONS: list[tuple[str, list[str], str]] = [
    ("the CAP theorem", ["cap theorem", "cap"], "CAP says a distributed store can guarantee only two of consistency, availability, and partition tolerance; since partitions happen, the real choice is C vs A during one."),
    ("how Raft elects a leader", ["raft"], "Followers wait a randomized timeout, become candidates, and ask for votes; a majority makes a leader, and randomized timeouts keep two candidates from splitting the vote forever."),
    ("SQL joins", ["sql join", "join"], "An inner join keeps only matching rows, a left join keeps every left row and fills nulls, a full outer join keeps both sides. Think of the Venn diagram."),
    ("how vaccines work", ["vaccine"], "A vaccine shows the immune system a harmless piece of a pathogen so it builds memory cells; a later real infection is recognized and cleared fast."),
    ("recursion", ["recursion", "recursive"], "A function that calls itself on a smaller input, with a base case that stops it; every recursion is a loop plus a stack."),
    ("Bayes theorem", ["bayes"], "Posterior is proportional to likelihood times prior: start from what you believed, weight it by how well each hypothesis explains the evidence."),
    ("TCP congestion control", ["tcp", "congestion"], "Slow start doubles the window each round trip, congestion avoidance grows it linearly, a loss halves it: AIMD keeps competing flows fair."),
    ("Dockerfile layer caching", ["docker"], "Each instruction is a cached layer keyed on its inputs; put things that change rarely first so a code change doesn't rebuild the dependency install."),
    ("vector clocks", ["vector clock"], "Every node keeps a counter per node; senders attach their vector, receivers take the element-wise max and bump their own, so you can tell ordered from concurrent events."),
    ("the bias-variance tradeoff", ["bias", "variance"], "Simple models underfit (high bias), flexible ones overfit (high variance); the sweet spot minimizes the sum, which is what regularization tunes."),
    ("how LoRA fine-tuning works", ["lora", "fine-tun"], "Freeze the base weights and train two small low-rank matrices whose product is added to a layer's weight; a few million trainable parameters instead of billions."),
    ("what a B-tree is", ["b-tree", "btree"], "A balanced tree with wide nodes sized to a disk page, so lookups touch only a handful of pages; that's why databases index with it."),
]

SEED_BACKGROUNDS = [
    "Here's my background for drafts: Berkeley EECS grad (Aug 2026), built an AI companion with a tool router, RAG memory and a LoRA-tuned classifier; interned at a startup doing backend Python and Postgres; comfortable with PyTorch, FastAPI, Docker.",
    "Background for cover letters: recent CS grad, two internships (data engineering with Spark and Airflow; a full-stack React/Node app), a capstone on LLM evaluation, TA for a systems course.",
    "For any application drafts: I'm a software engineer with 3 years in payments (Go, Kafka, Kubernetes), led an on-call rotation, moved into ML platform work last year (feature store, model serving).",
]


def history_for(category: str, rng: random.Random, message: str = "") -> list[dict]:
    """Prior turns a teacher trace needs to be answerable. A save-this
    message is only a tool call if something was just explained; a draft
    request is only a call if a background exists. Other categories get no history.

    The seeded explanation must be about what the message names: the pilot
    generated "save the one about SQL joins" against a random history about
    the CAP theorem, and the teacher correctly refused ("that's not what we
    discussed") - a refusal that would have trained the student to refuse.
    """
    if category == "save_learning_item":
        low = message.lower()
        matches = [e for e in SEED_EXPLANATIONS if any(k in low for k in e[1])]
        topic, _keys, text = rng.choice(matches) if matches else rng.choice(SEED_EXPLANATIONS)
        return [{"role": "user", "content": f"can you explain {topic}?"}, {"role": "assistant", "content": text}]
    if category == "draft_application_material":
        return [{"role": "user", "content": rng.choice(SEED_BACKGROUNDS)}, {"role": "assistant", "content": "Got it, I'll use that as your background for drafts."}]
    return []


def random_today(rng: random.Random) -> str:
    """A varied 'today' per trace so the student learns date arithmetic, not one calendar."""
    base = datetime.fromisoformat("2026-01-05T08:00:00-08:00")
    dt = base + timedelta(days=rng.randrange(0, 360), hours=rng.randrange(0, 14), minutes=rng.choice([0, 15, 30, 45]))
    return dt.isoformat()


@dataclass
class Trace:
    message: str
    category: str
    expect: str
    today: str
    calls: list[dict]  # [{"name", "args", "result"}]
    reply: str
    history: list[dict] = field(default_factory=list)  # prior turns the teacher saw (a save-this needs something to save)


def record_trace(
    backend, schemas: list[dict], message: str, category: str, expect: str, today: str, history: list[dict] | None = None,
) -> Trace:
    """Run the teacher's real tool loop against the fake registry and keep
    (call, result) pairs plus the final reply - everything a student row
    needs. Scripted results are the defaults, echoing arguments back."""
    registry = FakeRegistry(schemas, today=today)
    history = history or []
    prior = [Message(role=h["role"], content=h["content"]) for h in history]
    reply = backend.respond_with_tools(specialist_system(today), prior, message, registry)
    calls = [{"name": c.name, "args": c.args, "result": r} for c, r in zip(registry.calls, registry.results, strict=True)]
    return Trace(message=message, category=category, expect=expect, today=today, calls=calls, reply=reply, history=history)


MULTI_STEP_CATEGORIES = {"find_then_act", "two_tools"}


def trace_is_clean(trace: Trace, schemas: list[dict], max_calls: int = 4) -> tuple[bool, str]:
    """A trace is training-worthy only if it agrees with its category: a
    tool category must call something and every call must be a real tool
    with its required arguments; a no-tool category must call nothing; a
    multi-step category must take more than one step (the pilot kept a
    find-then-act trace that listed and then asked a question - a perfectly
    good answer, but it teaches list-then-stop, which is the exact failure
    the zero-shot student already has)."""
    by_name = {s["name"]: s for s in schemas}
    if trace.expect == "none" and trace.calls:
        return False, "called a tool in a no-tool category"
    if trace.expect == "tool" and not trace.calls:
        return False, "no call in a tool category"
    if len(trace.calls) > max_calls:
        return False, f"more than {max_calls} calls"
    if trace.category in MULTI_STEP_CATEGORIES and len(trace.calls) < 2:
        return False, f"{trace.category} needs more than one call"
    if any("error" in c["result"] for c in trace.calls if isinstance(c["result"], dict)):
        return False, "a call was rejected by the registry"
    for c in trace.calls:
        schema = by_name.get(c["name"])
        if schema is None:
            return False, f"unknown tool {c['name']}"
        req = schema.get("input_schema", {}).get("required", [])
        if any(k not in c["args"] for k in req):
            return False, f"{c['name']} missing required args"
    if not trace.reply.strip():
        return False, "empty reply"
    return True, ""


def trace_to_rows(trace: Trace, tools: list[dict]) -> list[dict]:
    """One mlx-lm chat row per assistant turn (mask-prompt trains only the
    last message). Qwen's template renders tool_calls and role:tool."""
    messages: list[dict] = [{"role": "system", "content": specialist_system(trace.today)}]
    messages += [{"role": h["role"], "content": h["content"]} for h in trace.history]
    messages.append({"role": "user", "content": trace.message})
    rows: list[dict] = []
    for c in trace.calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": c["name"], "arguments": c["args"]}}]})
        rows.append({"messages": list(messages), "tools": tools})
        messages.append({"role": "tool", "content": json.dumps(c["result"], default=str)})
    messages.append({"role": "assistant", "content": trace.reply})
    rows.append({"messages": list(messages), "tools": tools})
    return rows


def row_token_lengths(rows: list[dict], repo: str = STUDENT_REPO) -> list[int]:
    """Token length of each training row under the student's own chat
    template. Loads the tokenizer only (no weights), so it is cheap enough
    to run at build time.

    This exists because a row longer than `--max-seq-length` is silently
    truncated by mlx-lm, and truncation lands on the *end* of the row -
    which with --mask-prompt is exactly the assistant turn being trained.
    A measured maximum turns that into a build-time warning instead of a
    training run that quietly learned from cut-off targets.
    """
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(repo)
    lengths = []
    for row in rows:
        out = tok.apply_chat_template(row["messages"], tools=row.get("tools"), add_generation_prompt=False)
        # transformers 5.x returns a BatchEncoding (dict); 4.x returned a list of ids.
        ids = out["input_ids"] if hasattr(out, "keys") else out
        lengths.append(len(ids))
    return lengths


def split_traces(traces: list[Trace], valid_frac: float = 0.1, seed: int = 7) -> tuple[list[Trace], list[Trace]]:
    rng = random.Random(seed)
    rows = list(traces)
    rng.shuffle(rows)
    n_valid = max(1, int(len(rows) * valid_frac))
    return rows[n_valid:], rows[:n_valid]


def write_mlx_dataset(out_dir: Path, train: list[Trace], valid: list[Trace], schemas: list[dict]) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tools = to_openai_tools(schemas)
    counts = {}
    for name, traces in (("train", train), ("valid", valid)):
        n = 0
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for t in traces:
                for row in trace_to_rows(t, tools):
                    f.write(json.dumps(row) + "\n")
                    n += 1
        counts[name] = n
    return counts
