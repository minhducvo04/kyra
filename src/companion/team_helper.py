"""Page helper: one local-model answer about where to find things on the Team page.

Local lead model only (the same single-flight backend as local answers); nothing is stored or sent elsewhere.
The code enforces the shape: at most five short lines, one clarifying question at most, and a pointer only to a
control id the page offered.
"""
import json
import re
import threading
import uuid
from pathlib import Path

from companion.local_lead_inference import LeadInferenceError
from companion.team_code_context import lookup
from companion.team_lead import _digest, _validated_result

MAX_LINES = 5
LINE_CHARS = 160
_CONTROL_LINE = re.compile(r'(?i)\(?\bcontrol:\s*#?([A-Za-z][A-Za-z0-9_-]{0,63})\)?\.?')
# The prompt's own field names never belong in an answer; a line naming one is the model echoing its instructions.
_PROMPT_ECHO = re.compile(r'\b(?:page_controls|capability_context|feature_guide|code_context)\b|\broute clarify\b')
CONTROL_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
_GUIDE = Path(__file__).resolve().parents[2] / 'docs' / 'feature-guide.md'
_RULES = (
    'You are the Team page helper. The owner asked the question below about using this page. capability_context holds '
    'page_controls (element id and visible label), a feature_guide excerpt and code_context excerpts from the page '
    'source. Explain which control to use and what to do, in at most five short sentences. If the need is unclear, '
    'use route clarify and ask exactly one short question. When one page control is the right place, end the answer '
    'with "Control: <id>" using an id from page_controls only.'
)


def _guide_excerpt(limit=1500):
    """The Team section of docs/feature-guide.md, bounded."""
    try:
        text = _GUIDE.read_text(encoding='utf-8')
    except OSError:
        return ''
    start = text.find('## Team')
    if start < 0:
        return ''
    end = text.find('\n## ', start + 3)
    return text[start:end if end > 0 else None].encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def _controls(controls):
    result = []
    for item in controls[:80]:
        ident, label = str(item.get('id', '')), ' '.join(str(item.get('label', '')).split())[:80]
        if CONTROL_ID.fullmatch(ident) and ident not in {c['id'] for c in result}:
            result.append({'id': ident, 'label': label})
    return result


def _context(code_root, question, controls):
    context = {'page_controls': controls, 'feature_guide': _guide_excerpt(),
               'code_context': lookup(code_root, question)}
    encoded = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    while len(encoded.encode('utf-8')) > 8192 and context['page_controls']:
        context['page_controls'].pop()
        encoded = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    return encoded


def shape(text, route, allowed):
    """Hard limits on the model's text: short lines, one question, a known control or none."""
    control = ''
    lines = []
    for raw in re.split(r'\n+|(?<=[.!?])\s+', text.strip()):
        line = re.sub(r'\*\*|__|`', '', raw).strip(' -*•\t')
        for match in _CONTROL_LINE.finditer(line):
            control = control or (match.group(1) if match.group(1) in allowed else '')
        line = ' '.join(_CONTROL_LINE.sub('', line).split()).strip(' -*•')
        if line and not _PROMPT_ECHO.search(line):
            lines.append(line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1].rstrip() + '…')
    clarify = route == 'clarify'
    if clarify:  # keep everything up to the first question, so exactly one is asked
        first = next((i for i, line in enumerate(lines) if line.endswith('?')), None)
        if first is not None:
            lines = lines[:first + 1][-MAX_LINES:]
    lines = lines[:MAX_LINES]
    if not control:  # a bare mention of exactly one offered id also counts
        named = {ident for ident in allowed if re.search(rf'(?<![\w-])#?{re.escape(ident)}(?![\w-])', ' '.join(lines))}
        control = named.pop() if len(named) == 1 else ''
    return {'lines': lines, 'control': control, 'clarify': clarify}


def ask(lead, code_root, question, controls, history=()):
    """One answer from the local lead model. Raises the lead's own errors when it is busy or unavailable."""
    controls = _controls(controls)
    turns = [dict(id=f'h{index}', role=turn['role'], text=turn['text'][:1000])
             for index, turn in enumerate(history[-6:]) if turn.get('role') in ('user', 'assistant') and turn.get('text')]
    capsule = dict(request=f'{_RULES}\n\nQuestion: {question}', sources=[], history=turns,
                   project=lead.project['project_id'], room='team-helper', request_id=uuid.uuid4().hex,
                   capability_context=_context(code_root, question, controls))
    capsule['capsule_sha256'] = _digest(capsule)
    with lead.backend.flight() as flight:
        result = flight.generate(capsule, cancel_event=threading.Event())
    try:
        output = _validated_result(result, capsule, lead.project)
    except (ValueError, TypeError, KeyError) as exc:
        raise LeadInferenceError('output_invalid') from exc
    return shape(output['text'], output['route'], {c['id'] for c in controls})
