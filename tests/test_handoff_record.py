"""The handoff record is one shape for every model; the validator is the rule, not the prompt."""
import pytest

from companion import handoff_record as hr

REV = 'a' * 40


def _record(**over):
    base = dict(task_id='run-42', title='Triage the Model3D diff', who='Qwen3.6-35B-A3B, worker', job_class='2', revision=REV,
                result='Mapped three files to Model3D and named its focused tests.',
                evidence='scripts/worker_shadow.py: 1/1 passed; pytest tests/test_model3d.py: 29 passed in 1.2s',
                read='docs/feature-guide.md rows 20 to 22; src/companion/model3d.py:40', unsure='Whether web/model3d.js counts as docs.',
                rule_check='AGENTS.md 2 (no data/ paths cited); test-impact (focused set, not the suite).')
    base.update(over)
    return hr.HandoffRecord(**base)


def test_render_then_parse_round_trips_every_section():
    record = _record(for_training='verdict corrected; rule: focused tests come from the guide row; fix: added tests/test_model3d_api.py')
    text = record.render()
    back = hr.load(text)
    assert back == record
    assert back.verdict == 'corrected'
    assert all(f'## {name}' in text for name in hr.SECTIONS)


def test_missing_section_or_header_is_rejected():
    text = _record().render().replace('## Unsure\nWhether web/model3d.js counts as docs.\n\n', '')
    with pytest.raises(hr.RecordError, match='Unsure'):
        hr.load(text)
    with pytest.raises(hr.RecordError, match='Who'):
        hr.load('# x: y\n\n## Result\nok\n')


def test_evidence_must_name_a_command_outcome_and_sections_must_not_be_empty():
    with pytest.raises(hr.RecordError, match='Evidence'):
        hr.validate(_record(evidence='I ran the tests and they looked fine.'))
    with pytest.raises(hr.RecordError, match='Read'):
        hr.validate(_record(read=''))
    assert hr.validate(_record(evidence='exit code 0 after ruff: All checks passed')).evidence


def test_for_training_needs_a_verdict_when_present():
    assert hr.validate(_record()).verdict is None
    with pytest.raises(hr.RecordError, match='verdict'):
        hr.validate(_record(for_training='the mentor liked it'))
    assert hr.validate(_record(for_training='Verdict: accept')).verdict == 'accept'
