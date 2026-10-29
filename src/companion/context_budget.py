"""One context rule for Kyra's local-model loops: fit the prompt inside the model's real window.

The hosted lanes are measured by scripts/context_hygiene.py; a local loop has no transcript to measure, so it
checks its own prompt before each call. History is kept in whole exchanges (a user turn and what followed it),
newest first. The newest exchange is never dropped: when it alone overflows, its messages keep head and tail.
Older exchanges that do not fit are folded into one fixed note (never a changing count, so the cached prompt
prefix survives the next turn). The token estimate is conservative (three characters a token, the fallback
companion.worker_shadow uses without a cached tokenizer), so a fitted prompt stays under the window.
"""
import json
import math

CHARS_PER_TOKEN = 3
DEFAULT_LOCAL_WINDOW = 32_768  # Qwen2.5 instruct max_position_embeddings, the default local model
TOOL_RESULT_CHARS = 6000       # full_bench.agent.HISTORY_OUTPUT_CHARS
MIN_MESSAGE_CHARS = 600        # the least a bounded message keeps (head and tail)
FOLD_NOTE = ("(Older messages of this conversation were folded away to fit the local model's context; "
             "ask the owner if one of them matters.)")


def estimate(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def window(backend, default=DEFAULT_LOCAL_WINDOW):
    """The loaded model's max_position_embeddings, else its cached config.json, else the default."""
    args = getattr(getattr(backend, '_model', None), 'args', None)
    value = getattr(args, 'max_position_embeddings', None)
    if isinstance(value, int) and value > 0:
        return value
    repo = getattr(backend, '_repo', None)
    if repo:
        try:
            from huggingface_hub import try_to_load_from_cache
            path = try_to_load_from_cache(repo, 'config.json')
            if isinstance(path, str):
                config = json.loads(open(path, encoding='utf-8').read())
                value = config.get('max_position_embeddings') or (config.get('text_config') or {}).get(
                    'max_position_embeddings')
                if isinstance(value, int) and value > 0:
                    return value
        except (ImportError, OSError, ValueError, AttributeError):
            pass
    return default


def history_budget(backend, *fixed_texts, reserve_tokens=0):
    """Tokens left for history: the window less the answer, the fixed prompt parts, the fold note, a margin and
    ``reserve_tokens`` (what a loop adds after this point)."""
    reserve = (getattr(backend, '_max_tokens', None) or 512) + estimate(FOLD_NOTE) + 256 + reserve_tokens
    return max(0, window(backend) - reserve - sum(estimate(text) for text in fixed_texts))


def bound_text(text, limit=TOOL_RESULT_CHARS):
    """Head and tail of a long text with the omission marked, never the middle; never longer than ``limit``."""
    if len(text) <= limit:
        return text
    keep = limit - len(f'\n... [{len(text)} chars omitted] ...\n')  # the widest the marker can be
    if keep <= 0:
        return text[:limit]
    head = keep * 2 // 3
    tail = keep - head
    return text[:head] + f'\n... [{len(text) - keep} chars omitted] ...\n' + text[len(text) - tail:]


def fit_history(turns, budget):
    """turns: [(role, text)]. Returns ([(index, text)] to keep in order, folded).

    Whole exchanges, newest first, while they fit; the newest is always kept, bounded if it alone overflows."""
    starts = [i for i, (role, _) in enumerate(turns) if role == 'user']
    if not turns:
        return [], False
    cuts = sorted({0, *starts})
    groups = [list(range(a, b)) for a, b in zip(cuts, cuts[1:] + [len(turns)], strict=True)]
    newest = groups.pop()
    texts = {i: turns[i][1] for i in newest}
    if sum(estimate(t) for t in texts.values()) > budget:
        share = max(MIN_MESSAGE_CHARS, budget * CHARS_PER_TOKEN // len(newest) - 80)
        texts = {i: bound_text(t, share) for i, t in texts.items()}
    used = sum(estimate(t) for t in texts.values())
    kept = []
    for group in reversed(groups):
        cost = sum(estimate(turns[i][1]) for i in group)
        if used + cost > budget:
            break
        kept = group + kept
        used += cost
    folded = len(kept) < sum(len(g) for g in groups)
    if folded:  # the kept history starts on a user turn, never a reply cut from its question
        while kept and turns[kept[0]][0] != 'user':
            kept = kept[1:]
    return [(i, turns[i][1]) for i in kept] + sorted(texts.items()), folded
