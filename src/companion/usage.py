"""Paid Anthropic usage: one ledger line per call and a daily cap on calls no human asked for.

Every send through `provider.GatedAnthropic` calls `check_cap()` before it goes out and `record()` when the reply
(or the stream's final message) comes back. Rows land in <data_dir>/usage/anthropic.jsonl. A call counts as
human-initiated only inside `human_initiated(True)`: the web app marks HTTP requests, the job worker marks the job
kinds the owner starts from the UI. Everything else (launchd scripts, the watcher's prepare jobs, honesty checks, CLI runs by
agents) is background and is refused once today's estimated spend reaches KYRA_API_DAILY_USD (default 3.00). Human
calls past the cap still go out, with a warning line in the log.
"""
import inspect
import json
import logging
import math
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# USD per million tokens (input, output), first-party API rates as of 2026-10-06. Longest prefix wins.
PRICES = {
    "claude-fable": (10.00, 50.00),
    "claude-mythos": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus": (5.00, 25.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet": (2.00, 10.00),
    "claude-haiku-5": (0.10, 0.50),
    "claude-haiku": (1.00, 5.00),
}
UNKNOWN_PRICE = (10.00, 50.00)  # an unknown model is priced at the top tier, so the cap errs toward refusing
CACHE_WRITE, CACHE_READ = 1.25, 0.10  # multiples of the input rate
_SKIP = ("companion.provider", "companion.llm", "companion.usage", "anthropic", "contextlib")

_human = ContextVar("kyra_human_initiated", default=False)
_lock = threading.Lock()


class SpendCapReached(RuntimeError):
    """A background paid call was refused because today's estimated spend reached the cap."""


@contextmanager
def human_initiated(flag=True):
    token = _human.set(bool(flag))
    try:
        yield
    finally:
        _human.reset(token)


def is_human():
    return _human.get()


def ledger_path():
    from companion.settings import get_settings
    return Path(get_settings().data_dir) / "usage" / "anthropic.jsonl"


DEFAULT_CAP_USD = 3.00
_warned = set()


def env_knob(name):
    """A knob from the process environment, else from the .env file Settings reads (its env_file, so a test run
    that blanks env_file never sees the developer's .env). Read here, not in settings.py, which is protected."""
    if name in os.environ:
        return os.environ[name]
    from companion.settings import Settings
    env_file = Settings.model_config.get("env_file")
    if not env_file or not Path(env_file).exists():
        return None
    from dotenv import dotenv_values
    return dotenv_values(env_file).get(name)


def daily_cap_usd():
    """KYRA_API_DAILY_USD, default 3.00; a non-numeric or negative value falls back to 3.00 and logs once."""
    raw = env_knob("KYRA_API_DAILY_USD")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_CAP_USD
    try:
        value = float(raw)
        if math.isfinite(value) and value >= 0:
            return value
    except ValueError:
        pass
    if raw not in _warned:
        _warned.add(raw)
        logger.warning("KYRA_API_DAILY_USD is not a non-negative number; using $%.2f", DEFAULT_CAP_USD)
    return DEFAULT_CAP_USD


daily_cap = daily_cap_usd


def price(model):
    for prefix in sorted(PRICES, key=len, reverse=True):
        if str(model).startswith(prefix):
            return PRICES[prefix]
    return UNKNOWN_PRICE


def estimate(model, input_tokens, output_tokens, cache_write=0, cache_read=0):
    rate_in, rate_out = price(model)
    total = (input_tokens + cache_write * CACHE_WRITE + cache_read * CACHE_READ) * rate_in + output_tokens * rate_out
    return round(total / 1_000_000, 6)


def caller():
    """module:function of the first frame outside the provider, the LLM wrapper and the SDK."""
    for frame in inspect.stack(context=0)[1:]:
        module = frame.frame.f_globals.get("__name__", "")
        if not module.startswith(_SKIP):
            return f"{module}:{frame.function}"
    return "unknown:unknown"


def _valid(row):
    usd = row.get("est_usd") if isinstance(row, dict) else None
    return (isinstance(row, dict) and isinstance(row.get("at"), str) and isinstance(usd, (int, float))
            and not isinstance(usd, bool) and math.isfinite(usd) and usd >= 0)


def read(path=None):
    """(valid rows, count of unreadable lines). Line by line, so one bad byte or field never hides or breaks the rest."""
    path = path or ledger_path()
    if not path.exists():
        return [], 0
    good, bad = [], 0
    for raw in path.read_bytes().splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if _valid(row):
            good.append(row)
        else:
            bad += 1
    return good, bad


def rows(path=None):
    return read(path)[0]


def spent(day=None, path=None):
    """(today's total, unreadable lines). An unreadable line could be today's, so callers must not treat it as $0."""
    day = (day or datetime.now().astimezone().date()).isoformat()
    good, bad = read(path)
    return round(sum(float(r["est_usd"]) for r in good if r["at"].startswith(day)), 6), bad


def check_cap(*, path=None, cap=None):
    """Raise SpendCapReached for a background call once today's spend reaches the cap, or when the ledger cannot be
    read in full (an unreadable row might be today's spend); a human call goes out with a warning either way."""
    try:
        cap = daily_cap() if cap is None else cap
        today, bad = spent(path=path)
        problem = f"{bad} unreadable ledger line(s)" if bad else None
    except Exception as exc:  # noqa: BLE001 - a broken ledger must fail closed for background, open for the owner
        today, problem = float("inf"), f"ledger unreadable ({type(exc).__name__})"
    if problem is None and today < cap:
        return
    reason = problem or f"spend today ${today:.2f} reached the ${cap:.2f} cap"
    if is_human():
        logger.warning("Anthropic %s; human call allowed", reason)
        return
    raise SpendCapReached(f"Daily Anthropic cap: {reason}; background call refused. "
                          "Raise KYRA_API_DAILY_USD, fix data/usage/anthropic.jsonl, or wait for tomorrow.")


def record(model, usage, *, who=None, path=None):
    """Append one ledger row; never raises, a failed write must not lose a reply already paid for."""
    def count(name):
        value = getattr(usage, name, 0) if usage is not None else 0
        return value if isinstance(value, int) else 0

    tokens_in, tokens_out = count("input_tokens"), count("output_tokens")
    cache_write, cache_read = count("cache_creation_input_tokens"), count("cache_read_input_tokens")
    row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "caller": who or caller(),
           "model": str(model), "input_tokens": tokens_in, "output_tokens": tokens_out,
           "cache_write_tokens": cache_write, "cache_read_tokens": cache_read,
           "est_usd": estimate(model, tokens_in, tokens_out, cache_write, cache_read), "human_initiated": is_human()}
    try:
        path = path or ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("Could not write the Anthropic usage ledger")
    return row
