"""Claims need native assistant attestations from the registered review session."""
import json
import os

import pytest

from companion.workflow_evidence import verify_evidence

HEAD = 'a' * 40


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


@pytest.fixture
def native(tmp_path):
    home = tmp_path / 'home'
    repo = tmp_path / 'repo'
    repo.mkdir()
    codex = home / '.codex/sessions/2026/09/27/rollout-2026-09-27T12-00-00-codex-session.jsonl'
    meta = {'type': 'session_meta', 'payload': {'id': 'codex-session', 'cwd': str(repo),
            'originator': 'Codex Desktop', 'source': 'desktop', 'name': 'Synthetic builder'}}
    write_rows(codex, [meta, codex_message(f'KYRA-CODE {HEAD} COMPLETE')])
    registry = home / '.claude/sessions/session.json'
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'sessionId': 'claude-session', 'name': 'Synthetic review',
        'cwd': str(repo), 'entrypoint': 'claude-desktop', 'kind': 'interactive',
        'pid': os.getpid(), 'messagingSocketPath': '/private/tmp/synthetic-socket', 'status': 'idle'}))
    claude = write_rows(home / '.claude/projects/synthetic/claude-session.jsonl',
                       [claude_message(f'KYRA-REVIEW {HEAD} APPROVE')])
    chats = [{'app': 'claude', 'id': 'claude-session', 'cwd': str(repo)},
             {'app': 'codex', 'id': 'codex-session', 'cwd': str(repo)}]
    evidence = [{'head_sha': HEAD, 'provider': 'claude', 'session_id': 'claude-session', 'role': 'review', 'verdict': 'approve'},
                {'head_sha': HEAD, 'provider': 'codex', 'session_id': 'codex-session', 'role': 'code', 'verdict': 'complete'}]
    return home, repo, codex, claude, chats, evidence


def codex_message(text, role='assistant'):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
            'content': [{'type': 'output_text', 'text': text}]}}


def claude_message(text, role='assistant', block='text', sid='claude-session'):
    return {'type': role, 'sessionId': sid, 'isSidechain': False,
            'message': {'role': role, 'content': [{'type': block, 'text': text}]}}


def test_native_independent_assistant_attestations_verify(native):
    home, _, _, _, chats, evidence = native
    result = verify_evidence(evidence, chats, HEAD, home=home)
    assert [(row.provider, row.verdict) for row in result] == [('claude', 'approve'), ('codex', 'complete')]


@pytest.mark.parametrize('role,block', [('user', 'text'), ('assistant', 'tool_result'), ('assistant', 'thinking')])
def test_user_tool_and_thinking_text_cannot_attest(native, role, block):
    home, _, _, claude, chats, evidence = native
    write_rows(claude, [claude_message(f'KYRA-REVIEW {HEAD} APPROVE', role=role, block=block)])
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


@pytest.mark.parametrize('text', [f'> KYRA-REVIEW {HEAD} APPROVE',
    f'```text\nKYRA-REVIEW {HEAD} APPROVE\n```', f'Example: KYRA-REVIEW {HEAD} APPROVE',
    f'"KYRA-REVIEW {HEAD} APPROVE"', f' KYRA-REVIEW {HEAD} APPROVE'])
def test_quoted_fenced_or_nonliteral_marker_is_not_authority(native, text):
    home, _, _, claude, chats, evidence = native
    write_rows(claude, [claude_message(text)])
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


def test_later_rejection_invalidates_earlier_approval(native):
    home, _, _, claude, chats, evidence = native
    with claude.open('a') as stream:
        stream.write(json.dumps(claude_message(f'KYRA-REVIEW {HEAD} REJECT')) + '\n')
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


def test_wrong_transcript_session_is_rejected(native):
    home, _, _, claude, chats, evidence = native
    write_rows(claude, [claude_message(f'KYRA-REVIEW {HEAD} APPROVE', sid='different')])
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


@pytest.mark.parametrize('change', ['unregistered', 'wrong_head', 'path_claim', 'wrong_cwd'])
def test_claim_cannot_select_another_session_head_or_path(native, change):
    home, _, _, _, chats, evidence = native
    if change == 'unregistered':
        chats = chats[1:]
    elif change == 'wrong_head':
        evidence[0]['head_sha'] = 'b' * 40
    elif change == 'path_claim':
        evidence[0]['source'] = '/private/tmp/forged.jsonl'
    else:
        chats[0]['cwd'] = '/private/tmp/different-repo'
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


def test_codex_user_message_cannot_complete_code(native):
    home, _, codex, _, chats, evidence = native
    meta = json.loads(codex.read_text().splitlines()[0])
    write_rows(codex, [meta, codex_message(f'KYRA-CODE {HEAD} COMPLETE', role='user')])
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


def test_native_transcript_symlink_to_candidate_is_rejected(native):
    home, repo, _, claude, chats, evidence = native
    forged = repo / 'forged.jsonl'
    forged.write_text(claude.read_text())
    claude.unlink()
    claude.symlink_to(forged)
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


def test_badly_formed_transcript_fails_closed(native):
    home, _, _, claude, chats, evidence = native
    claude.write_text('{not-json\n')
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)


def test_session_id_cannot_traverse_directories(native):
    home, _, _, _, chats, evidence = native
    evidence[0]['session_id'] = '../../forged'
    chats[0]['id'] = '../../forged'
    with pytest.raises(ValueError):
        verify_evidence(evidence, chats, HEAD, home=home)
