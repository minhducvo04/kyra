"""Short Team chat text; full results belong in team_log."""
import re
import unicodedata

LIMITS = {'reply': (240, 2), 'interview': (1200, 12), 'report': (600, 6)}


def _chat_character(char):
    # Joiners carry emoji/linguistic meaning; variation selectors are printable
    # marks. Other controls still cannot become chat text.
    return char in '\n\u200c\u200d' or char.isprintable()


def chat_line(text, *, limit=240, lines=2):
    """Keep fitting text; otherwise take its first sentence/line and add an ellipsis."""
    # JSON callers can supply explicit UTF-16 pairs. Decode pairs together, never
    # publish half a pair; normal astral characters and decomposed marks survive.
    if not str(text or '').strip():
        return ''  # nothing to say stays empty (a failed answer shows no text)
    text = text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')
    text = ''.join(c if _chat_character(c) else ' ' for c in text).strip()
    if limit <= 0 or lines <= 0:
        return ''
    if not text:
        return '…'  # only control characters: say something was there
    if len(text) <= limit and len(text.splitlines()) <= lines:
        return text
    budget = limit - 1
    # A 2-line chat line is the first sentence (the outcome). Larger budgets (report card, interview)
    # keep whole sentences/lines while they fit; cut inside a sentence only when the first is too long.
    pieces = [piece for piece in re.split(r'(\n|(?<=[.!?])\s)', text)] if lines > 2 else []
    kept = ''
    for index in range(0, len(pieces), 2):
        candidate = kept + (pieces[index - 1] if index else '') + pieces[index]
        if len(candidate.rstrip()) > budget or len(candidate.rstrip().splitlines()) > lines:
            break
        kept = candidate
    if kept.strip():
        return kept.rstrip() + '…'
    first = re.split(r'\n|(?<=[.!?])\s', text, maxsplit=1)[0].rstrip()
    if len(first) > budget:
        cut = budget
        # If the next character combines with the last base, drop that entire
        # cluster. Cutting between a Vietnamese base and its marks changes it.
        while cut > 0 and unicodedata.category(first[cut]).startswith('M'):
            cut -= 1
        fragment = first[:cut]
        if cut < len(first) and not first[cut].isspace():
            boundary = max(fragment.rfind(' '), fragment.rfind('\t'))
            if boundary >= 0:
                fragment = fragment[:boundary]
        first = fragment.rstrip()
    return first + '…'


def result_chat(summary, chat=None, *, limit=240, lines=2):
    """Optional chat is advisory: invalid chat falls back to the valid summary."""
    if (isinstance(chat, str) and chat.strip() and len(chat) <= limit
            and len(chat.splitlines()) <= lines
            and all(_chat_character(c) for c in chat)):
        try:
            chat.encode('utf-8')
        except UnicodeError:
            pass
        else:
            return chat
    return chat_line(summary, limit=limit, lines=lines)
