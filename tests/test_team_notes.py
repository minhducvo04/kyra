"""Claude acceptance for one selected Team result saved as an Obsidian note (2026-09-28), written before the build.

Contract: a private design note as amended after Codex's review and Home's ACK. TeamStore.note_preview
renders one saved result of the room's latest completed run into exact Markdown bytes without writing anything;
TeamStore.note_confirm publishes those bytes create-only at the absolute configured
<configured vault>/Generated Team/<run>-<message>.md through verified directory handles. Selected
metadata is validated, text stays verbatim, `<%` is refused and every variable string sits in a longer `text` fence.
Scratch DATA_DIR, a fictional vault folder and fictional text only; no Obsidian, Sync, native, provider or network call.
"""
import hashlib
import json
import os
import re
import signal
import stat
import time
import tracemalloc
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from markdown_it import MarkdownIt

from companion import paths, team_chat
from companion.errors import ApiError
from companion.settings import get_settings
from companion.workflow_control import WorkflowStore

ROOM, RUN, OLD_RUN, NEWER_RUN = '0123456789abcdef' * 2, 'fedcba9876543210' * 2, '1122334455667788' * 2, '9a' * 16
USER, PLAN, BUILD, REVIEW, OLD_RESULT = 'aa' * 16, 'bb' * 16, 'cc' * 16, 'dd' * 16, 'ee' * 16
SAVED = 1700000000.75  # 2023-11-14T22:13:20Z once floored to whole seconds
TITLE = '  Fictional  launch notes  '
TEXT = '  Plan: tidy the fictional docs.\n\n    Keep `code` spans.\nTiêu đề 日本語 🚀\n\n'
MISSING = object()


def completed_run(run_id, created_at=SAVED - 300):
    return {'id': run_id, 'status': 'completed', 'stage': 'review', 'phase': 'terminal', 'attempt': 0,
            'reason': 'Build and independent review finished for the same workspace revision.',
            'created_at': created_at, 'expires_at': created_at + 1800, 'stop_requested': False,
            'role_lease': None, 'write_lease': False, 'release_outcome': 'completed', 'revision': 'ab' * 20,
            'tasks': [{'id': f'{run_id}-0', 'assignee': 'codex', 'status': 'completed', 'title': 'Fictional task',
                       'instruction': 'Tidy the fictional docs.'}],
            'submitted_config': None, 'binding': 'cd' * 32,
            'learning': {'status': 'none', 'reason': 'No curated example.'}}


def result(message_id, author, stage, text, created_at, run_id=RUN):
    return {'id': message_id, 'author': author, 'text': text, 'created_at': created_at, 'run_id': run_id,
            'kind': 'result', 'stage': stage,
            'assurance': 'Assignment-correlated reply; not authenticated against same-user processes.'}


def state(controller_path):
    """A writer-valid v2 store: one room whose only run completed plan, build and review."""
    return {'version': 2, 'controller_path': controller_path, 'rooms': {ROOM: {
        'room': {'id': ROOM, 'title': TITLE, 'local_only': False, 'created_at': SAVED - 600, 'updated_at': SAVED + 120},
        'runs': [completed_run(RUN)],
        'messages': [{'id': USER, 'author': 'user', 'text': 'Fictional instruction: tidy the docs.',
                      'created_at': SAVED - 300, 'request_id': 'req-fictional', 'run_id': RUN, 'shared': True},
                     result(PLAN, 'claude', 'plan', 'Plan: one fictional task.', SAVED),
                     result(BUILD, 'codex', 'build', 'Built the fictional task.', SAVED + 60),
                     result(REVIEW, 'claude', 'review', TEXT, SAVED + 120)]}}}


def save(store, body):
    store.path.write_text(json.dumps(body), encoding='utf-8')  # lone surrogates and non-finite numbers survive JSON
    store.path.chmod(0o600)


def load(store):
    return json.loads(store.path.read_text(encoding='utf-8'))


def message(body, message_id):
    return next(m for m in body['rooms'][ROOM]['messages'] if m['id'] == message_id)


def fence(value):
    return '`' * max(3, max(map(len, re.findall('`+', value)), default=0) + 1)


def expected(title=TITLE, text=TEXT, *, stage='review', author='claude', saved='2023-11-14T22:15:20Z', run_id=RUN):
    """Independent reference for the contract's exact bytes."""
    a, b = fence(title), fence(text)
    return (f'# Kyra Team result\n\nRun {run_id} · stage {stage} · author {author} · saved {saved}\n'
            'Selected local snapshot from Kyra Team; not live sync.\n\n'
            f'Room title:\n\n{a} text\n{title}\n{a}\n\nResult:\n\n{b} text\n{text}\n{b}\n')


def sha(note):
    return hashlib.sha256(note.encode('utf-8')).hexdigest()


def listing(root):
    """Every entry with its type, permission bits and, for files, size and mtime; directory times are excluded."""
    rows = {}
    for top, directories, files in os.walk(root):
        for name in directories + files:
            path = os.path.join(top, name)
            info = os.lstat(path)
            regular = stat.S_ISREG(info.st_mode)
            rows[os.path.relpath(path, root)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                                                 info.st_size if regular else None,
                                                 info.st_mtime_ns if regular else None)
    return rows


def refused(call, *args, **kwargs):
    with pytest.raises(ApiError) as caught:
        call(*args, **kwargs)
    return caught.value.status_code, caught.value.code


@contextmanager
def deadline(seconds=5):
    """Turn a blocking open or read (for example of a FIFO) into a failure instead of a hung suite."""
    def expire(signum, frame):
        raise TimeoutError('the note call blocked')
    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    kyra = data / 'vault-root' / 'obsidian' / 'Kyra'
    kyra.mkdir(parents=True)
    (kyra / 'Home.md').write_text('Fictional vault home.\n', encoding='utf-8')
    modules = [paths, team_chat]
    try:  # imported before DATA_DIR moves, so an import-time binding cannot capture this test's folder
        from companion import team_notes
        modules.append(team_notes)
    except ImportError:  # the immutable red base has no note module yet
        pass
    for module in modules:
        monkeypatch.setattr(module, 'DATA_DIR', data, raising=False)
    monkeypatch.setenv('KYRA_OBSIDIAN_VAULT', str(kyra))  # the vault is a setting since 2026-10-01
    get_settings.cache_clear()
    store = team_chat.TeamStore(tmp_path / 'team', controller=WorkflowStore(tmp_path / 'control' / 'control.json'))
    store.root.mkdir(mode=0o700)
    save(store, state(str(store.controller.path)))
    yield SimpleNamespace(tmp=tmp_path, data=data, kyra=kyra, generated=kyra / 'Generated Team', store=store,
                          target=kyra / 'Generated Team' / f'{RUN}-{REVIEW}.md')
    get_settings.cache_clear()


def test_preview_renders_the_exact_selected_note_and_writes_nothing(vault):
    before, note = listing(vault.tmp), expected()
    preview = vault.store.note_preview(ROOM, REVIEW)
    assert preview == {'room_id': ROOM, 'run_id': RUN, 'message_id': REVIEW, 'destination': str(vault.target),
                       'note': note, 'note_sha256': sha(note), 'byte_count': len(note.encode('utf-8')),
                       'target': 'absent'}
    assert os.path.isabs(preview['destination'])  # the configured DATA_DIR, never a literal relative data/ path
    assert listing(vault.tmp) == before  # no Generated Team, temporary, lock or Team state change


@pytest.mark.parametrize(('message_id', 'stage', 'author', 'text', 'saved'), [
    (PLAN, 'plan', 'claude', 'Plan: one fictional task.', '2023-11-14T22:13:20Z'),
    (BUILD, 'build', 'codex', 'Built the fictional task.', '2023-11-14T22:14:20Z'),
    (REVIEW, 'review', 'claude', TEXT, '2023-11-14T22:15:20Z')], ids=['plan', 'build', 'review'])
def test_each_result_renders_its_own_stage_author_and_saved_time(vault, message_id, stage, author, text, saved):
    preview = vault.store.note_preview(ROOM, message_id)  # the run itself ended at stage review
    assert preview['note'] == expected(text=text, stage=stage, author=author, saved=saved)
    assert preview['destination'] == str(vault.generated / f'{RUN}-{message_id}.md')


def test_preview_bytes_do_not_follow_the_clock(vault, monkeypatch):
    first = vault.store.note_preview(ROOM, REVIEW)
    monkeypatch.setattr(time, 'time', lambda: 4102444800.0)
    assert vault.store.note_preview(ROOM, REVIEW) == first


def test_confirm_publishes_exactly_the_previewed_bytes_once(vault):
    preview = vault.store.note_preview(ROOM, REVIEW)
    before = listing(vault.tmp)
    saved = vault.store.note_confirm(ROOM, REVIEW, expected_note_sha256=preview['note_sha256'])
    assert saved == {'saved': True, 'created': True, 'destination': str(vault.target),
                     'note_sha256': preview['note_sha256']}
    info, folder = vault.target.lstat(), vault.generated.lstat()
    assert (stat.S_ISREG(info.st_mode), stat.S_IMODE(info.st_mode), info.st_nlink) == (True, 0o600, 1)
    assert (stat.S_ISDIR(folder.st_mode), stat.S_IMODE(folder.st_mode)) == (True, 0o700)
    assert vault.target.read_bytes() == preview['note'].encode('utf-8')
    after = listing(vault.tmp)
    added = {os.path.relpath(vault.generated, vault.tmp), os.path.relpath(vault.target, vault.tmp)}
    assert set(after) - set(before) == added
    assert {key: after[key] for key in before} == before  # Team state and every other file untouched
    assert vault.store.note_preview(ROOM, REVIEW)['target'] == 'identical'


def test_a_repeat_or_lost_response_retry_is_a_no_op(vault):
    note = expected()
    first = vault.store.note_confirm(ROOM, REVIEW, expected_note_sha256=sha(note))  # no preview state is needed
    before, identity = listing(vault.tmp), (vault.target.stat().st_ino, vault.target.stat().st_mtime_ns)
    again = vault.store.note_confirm(ROOM, REVIEW, expected_note_sha256=sha(note))
    assert (first['created'], again) == (True, {**first, 'created': False})
    assert (vault.target.stat().st_ino, vault.target.stat().st_mtime_ns) == identity
    assert listing(vault.tmp) == before
    assert os.listdir(vault.generated) == [vault.target.name]  # no temporary left behind


def test_an_identical_existing_note_is_reused_whatever_its_mode(vault):
    vault.generated.mkdir(mode=0o755)
    vault.target.write_bytes(expected().encode('utf-8'))
    vault.target.chmod(0o644)
    before = listing(vault.tmp)
    assert vault.store.note_preview(ROOM, REVIEW)['target'] == 'identical'
    saved = vault.store.note_confirm(ROOM, REVIEW, expected_note_sha256=sha(expected()))
    assert saved['created'] is False
    assert listing(vault.tmp) == before  # mode, bytes and folder unchanged


@pytest.mark.parametrize('existing', ['owner edit', 'longer', 'shorter'])
def test_a_different_existing_note_is_a_conflict_and_is_never_changed(vault, existing):
    note = expected()
    content = {'owner edit': 'Owner edit: fictional canary.\n', 'longer': note + 'Owner addition.\n',
               'shorter': note[:-1]}[existing].encode('utf-8')
    vault.generated.mkdir(mode=0o700)
    vault.target.write_bytes(content)
    vault.target.chmod(0o600)
    before = listing(vault.tmp)
    assert vault.store.note_preview(ROOM, REVIEW)['target'] == 'different'
    assert refused(vault.store.note_confirm, ROOM, REVIEW, expected_note_sha256=sha(note)) == (409, 'note_conflict')
    assert vault.target.read_bytes() == content
    assert listing(vault.tmp) == before


def test_a_huge_existing_target_is_compared_with_a_bounded_read(vault):
    vault.generated.mkdir(mode=0o700)
    with open(vault.target, 'wb') as sparse:
        sparse.truncate(256 * 1024 * 1024)  # sparse: no disk use, but an unbounded read allocates all of it
    tracemalloc.start()
    try:
        assert vault.store.note_preview(ROOM, REVIEW)['target'] == 'different'
        code = refused(vault.store.note_confirm, ROOM, REVIEW, expected_note_sha256=sha(expected()))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert code == (409, 'note_conflict')
    assert peak < 32 * 1024 * 1024
    assert vault.target.stat().st_size == 256 * 1024 * 1024


@pytest.mark.parametrize('change', ['text', 'title', 'saved time'])
def test_a_changed_selection_is_stale_and_creates_nothing(vault, change):
    preview = vault.store.note_preview(ROOM, REVIEW)
    body = load(vault.store)
    if change == 'title':
        body['rooms'][ROOM]['room']['title'] = 'Renamed fictional notes'
    else:
        message(body, REVIEW)['text' if change == 'text' else 'created_at'] = (
            'Edited fictional result.' if change == 'text' else SAVED + 999)
    save(vault.store, body)
    before = listing(vault.tmp)
    assert refused(vault.store.note_confirm, ROOM, REVIEW,
                   expected_note_sha256=preview['note_sha256']) == (409, 'note_stale')
    assert listing(vault.tmp) == before


def test_unrelated_room_activity_keeps_an_unchanged_selection_valid(vault):
    preview = vault.store.note_preview(ROOM, REVIEW)
    body = load(vault.store)
    room = body['rooms'][ROOM]
    room['runs'].append({'id': NEWER_RUN, 'status': 'queued', 'stage': 'plan', 'phase': 'pending', 'attempt': 0,
                         'reason': 'Waiting for the planner.', 'created_at': SAVED + 500, 'expires_at': SAVED + 2300,
                         'stop_requested': False, 'role_lease': None, 'write_lease': False, 'tasks': [],
                         'revision': None, 'submitted_config': None, 'binding': 'ef' * 32})
    room['messages'].append({'id': '9b' * 16, 'author': 'user', 'text': 'A newer fictional instruction.',
                             'created_at': SAVED + 500, 'request_id': 'req-newer', 'run_id': NEWER_RUN, 'shared': True})
    room['room']['updated_at'] = SAVED + 500
    save(vault.store, body)
    # Preview offers only the latest run once it completes; confirm needs only the unchanged, still-completed result.
    assert refused(vault.store.note_preview, ROOM, REVIEW) == (409, 'not_eligible')
    saved = vault.store.note_confirm(ROOM, REVIEW, expected_note_sha256=preview['note_sha256'])
    assert saved['created'] is True
    assert vault.target.read_bytes() == preview['note'].encode('utf-8')


def test_other_messages_are_neither_read_into_the_note_nor_validated(vault):
    body = load(vault.store)
    body['rooms'][ROOM]['messages'].insert(0, {'id': '9c' * 16, 'author': 'user', 'text': 'Legacy \ud800 <% x %>',
                                               'created_at': SAVED - 900, 'run_id': None, 'shared': False})
    save(vault.store, body)
    assert vault.store.note_preview(ROOM, REVIEW)['note'] == expected()


def ineligible(body, case):
    room = body['rooms'][ROOM]
    run = room['runs'][0]
    if case == 'older completed run':
        room['runs'].insert(0, completed_run(OLD_RUN, created_at=SAVED - 5000))
        room['messages'].insert(0, result(OLD_RESULT, 'claude', 'review', 'Older fictional review.', SAVED - 4000,
                                          run_id=OLD_RUN))
        return OLD_RESULT
    if case == 'user message':
        return USER
    run.update({'needs input': dict(status='needs_input', stage='plan', release_outcome='needs_input'),
                'still building': dict(status='building', stage='build', phase='waiting', role_lease='codex',
                                       write_lease=True, release_outcome=None),
                'stopped': dict(status='stopped', release_outcome='stopped')}[case])
    if run.get('release_outcome') is None:
        run.pop('release_outcome')
    return PLAN


@pytest.mark.parametrize('case', ['user message', 'older completed run', 'needs input', 'still building', 'stopped'])
def test_only_results_of_the_latest_completed_run_are_offered(vault, case):
    body = load(vault.store)
    message_id = ineligible(body, case)
    save(vault.store, body)
    before = listing(vault.tmp)
    assert refused(vault.store.note_preview, ROOM, message_id) == (409, 'not_eligible')
    if case != 'older completed run':  # an unchanged completed result stays confirmable (see the activity test)
        digest = sha(expected(text=message(body, message_id)['text'], stage='plan'))
        # Home's correction: a run that is not (or no longer) completed makes a confirm stale; a non-result never fits.
        code = 'not_eligible' if case == 'user message' else 'note_stale'
        assert refused(vault.store.note_confirm, ROOM, message_id, expected_note_sha256=digest) == (409, code)
    assert listing(vault.tmp) == before


def test_a_run_that_stops_being_completed_after_preview_makes_confirm_stale(vault):
    preview = vault.store.note_preview(ROOM, REVIEW)
    body = load(vault.store)
    body['rooms'][ROOM]['runs'][0].update(status='uncertain', reason='A shared claim needs inspection.')
    save(vault.store, body)
    before = listing(vault.tmp)
    assert refused(vault.store.note_confirm, ROOM, REVIEW,
                   expected_note_sha256=preview['note_sha256']) == (409, 'note_stale')
    assert listing(vault.tmp) == before


@pytest.mark.parametrize(('room_id', 'message_id'), [
    pytest.param(ROOM, 'ee' * 16, id='unknown-message'), pytest.param('9f' * 16, REVIEW, id='unknown-room'),
    pytest.param(ROOM, '../' + REVIEW, id='path-message'), pytest.param('../' + ROOM, REVIEW, id='path-room'),
    pytest.param(ROOM, REVIEW.upper(), id='uppercase-message')])
def test_unknown_or_malformed_ids_are_not_found(vault, room_id, message_id):
    before = listing(vault.tmp)
    with pytest.raises((LookupError, ApiError)) as caught:
        vault.store.note_preview(room_id, message_id)
    assert getattr(caught.value, 'status_code', 404) == 404
    with pytest.raises((LookupError, ApiError)) as caught:
        vault.store.note_confirm(room_id, message_id, expected_note_sha256=sha(expected()))
    assert getattr(caught.value, 'status_code', 404) == 404
    assert listing(vault.tmp) == before


@pytest.mark.parametrize(('field', 'value'), [
    pytest.param('text', 'Legacy \ud800 text', id='text-lone-surrogate'),
    pytest.param('text', 'Legacy \x00 text', id='text-nul'), pytest.param('text', ' \n ', id='text-blank'),
    pytest.param('text', 'x' * 16001, id='text-over-16000'), pytest.param('text', 7, id='text-number'),
    pytest.param('text', MISSING, id='text-missing'), pytest.param('author', '<iframe>', id='author-markup'),
    pytest.param('author', MISSING, id='author-missing'), pytest.param('stage', '```', id='stage-fence'),
    pytest.param('stage', MISSING, id='stage-missing'),
    pytest.param('created_at', float('inf'), id='time-infinite'),
    pytest.param('created_at', float('nan'), id='time-nan'),
    pytest.param('created_at', 1e12, id='time-year-33658'),
    pytest.param('created_at', '1700000000', id='time-string'), pytest.param('created_at', MISSING, id='time-missing'),
    pytest.param('title', 'Legacy \ud800 title', id='title-lone-surrogate'),
    pytest.param('title', 'x' * 101, id='title-over-100'), pytest.param('title', ' ', id='title-blank')])
def test_malformed_legacy_metadata_or_text_is_refused_before_rendering(vault, field, value):
    body = load(vault.store)
    holder = body['rooms'][ROOM]['room'] if field == 'title' else message(body, REVIEW)
    if value is MISSING:
        del holder[field]
    else:
        holder[field] = value
    save(vault.store, body)
    before = listing(vault.tmp)
    assert refused(vault.store.note_preview, ROOM, REVIEW) == (409, 'invalid_text')
    # Validation precedes the digest check, so even a wrong digest still reports the fixed text refusal.
    assert refused(vault.store.note_confirm, ROOM, REVIEW, expected_note_sha256='0' * 64) == (409, 'invalid_text')
    assert listing(vault.tmp) == before


def test_a_path_like_run_id_never_reaches_a_file_name(vault):
    escape = '../../../escape'
    body = load(vault.store)
    body['rooms'][ROOM]['runs'][0]['id'] = escape
    for item in body['rooms'][ROOM]['messages']:
        item['run_id'] = escape
    save(vault.store, body)
    before = listing(vault.tmp)
    assert refused(vault.store.note_preview, ROOM, REVIEW) == (409, 'invalid_text')
    naive = sha(expected(run_id=escape))  # what an unvalidated renderer would have produced
    assert refused(vault.store.note_confirm, ROOM, REVIEW, expected_note_sha256=naive) == (409, 'invalid_text')
    assert listing(vault.tmp) == before
    assert not [name for _, _, files in os.walk(vault.tmp) for name in files if 'escape' in name]


@pytest.mark.parametrize('field', ['text', 'title'])
def test_templater_tokens_are_refused_before_any_write(vault, field):
    body = load(vault.store)
    if field == 'title':
        body['rooms'][ROOM]['room']['title'] = 'Fictional <% tp.date.now() %> title'
    else:
        message(body, REVIEW)['text'] = 'Fictional result <%* tR += "ran" %> here.'
    save(vault.store, body)
    before = listing(vault.tmp)
    assert refused(vault.store.note_preview, ROOM, REVIEW) == (409, 'plugin_token')
    title = body['rooms'][ROOM]['room']['title']
    naive = sha(expected(title=title, text=message(body, REVIEW)['text']))
    assert refused(vault.store.note_confirm, ROOM, REVIEW, expected_note_sha256=naive) == (409, 'plugin_token')
    assert listing(vault.tmp) == before


def test_hostile_markdown_stays_literal_inside_two_text_fences(vault):
    title = '[[Home]] <b>bold</b> ``tick``'
    text = ('![[Home]] [[Home|alias]] <iframe src="https://example.invalid"></iframe> <b>bold</b>\n'
            '[click](javascript:alert(1)) %%hidden%% ==mark== #tag ^block\nfield:: value\n'
            '```dataviewjs\ndv.paragraph("ran")\n```\n`````\n---\ntitle: not frontmatter\n---\n')
    body = load(vault.store)
    body['rooms'][ROOM]['room']['title'] = title
    message(body, REVIEW)['text'] = text
    save(vault.store, body)
    note = vault.store.note_preview(ROOM, REVIEW)['note']
    assert note == expected(title=title, text=text)
    tokens = MarkdownIt('commonmark').parse(note)
    # CommonMark trims the info string; markdown-it keeps the raw " text" after the fence and trims when rendering.
    assert [(token.info.strip(), token.content) for token in tokens if token.type == 'fence'] == [
        ('text', title + '\n'), ('text', text + '\n')]
    assert {token.type for token in tokens} <= {'heading_open', 'heading_close', 'paragraph_open', 'paragraph_close',
                                                'inline', 'fence'}
    assert {child.type for token in tokens if token.type == 'inline' for child in token.children} <= {
        'text', 'softbreak'}


def unsafe_parent(vault, case):
    canary = vault.tmp / 'canary'
    if case == 'Kyra missing':
        vault.kyra.rename(vault.tmp / 'moved-kyra')
        return canary
    if case == 'Generated Team is a file':
        vault.generated.write_text('not a folder\n', encoding='utf-8')
        return canary
    component = {'vault-root': vault.data / 'vault-root', 'obsidian': vault.data / 'vault-root' / 'obsidian',
                 'Kyra': vault.kyra, 'Generated Team': vault.generated}[case]
    rest = os.path.relpath(vault.generated, component)
    decoy = canary / case
    (decoy / rest).mkdir(parents=True)  # following the link would find a complete, writable-looking path
    if component.exists():
        component.rename(vault.tmp / f'moved-{case}')
    component.symlink_to(decoy, target_is_directory=True)
    return canary


@pytest.mark.parametrize('case', ['vault-root', 'obsidian', 'Kyra', 'Generated Team', 'Kyra missing',
                                  'Generated Team is a file'])
def test_unsafe_vault_parents_are_refused_without_writing_anywhere(vault, case):
    canary = unsafe_parent(vault, case)
    before, canary_before = listing(vault.tmp), listing(canary)
    assert refused(vault.store.note_preview, ROOM, REVIEW) == (409, 'unsafe_destination')
    assert refused(vault.store.note_confirm, ROOM, REVIEW,
                   expected_note_sha256=sha(expected())) == (409, 'unsafe_destination')
    assert listing(vault.tmp) == before and listing(canary) == canary_before


@pytest.mark.parametrize('case', ['symlink to identical bytes', 'dangling symlink', 'directory', 'fifo'])
def test_unsafe_targets_are_refused_without_following_or_blocking(vault, case):
    outside = vault.tmp / 'outside.md'
    vault.generated.mkdir(mode=0o700)
    if case == 'symlink to identical bytes':
        outside.write_bytes(expected().encode('utf-8'))
        vault.target.symlink_to(outside)
    elif case == 'dangling symlink':
        vault.target.symlink_to(outside)
    elif case == 'directory':
        vault.target.mkdir()
    else:
        os.mkfifo(vault.target)
    before = listing(vault.tmp)
    with deadline():
        assert refused(vault.store.note_preview, ROOM, REVIEW) == (409, 'unsafe_destination')
        assert refused(vault.store.note_confirm, ROOM, REVIEW,
                       expected_note_sha256=sha(expected())) == (409, 'unsafe_destination')
    assert listing(vault.tmp) == before  # the dangling link's target was not created either


def test_a_parent_swapped_at_temporary_creation_never_reaches_a_canary(vault, monkeypatch):
    vault.generated.mkdir(mode=0o700)
    preview = vault.store.note_preview(ROOM, REVIEW)
    canary, moved = vault.tmp / 'canary', vault.kyra / 'Moved Team'
    canary.mkdir()
    real_open, swapped = os.open, []

    def swap_then_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and not swapped:  # a same-user rename plus link, just before the temporary exists
            vault.generated.rename(moved)
            vault.generated.symlink_to(canary, target_is_directory=True)
            swapped.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', swap_then_open)
    try:
        vault.store.note_confirm(ROOM, REVIEW, expected_note_sha256=preview['note_sha256'])
    except ApiError as refusal:
        assert refusal.code == 'unsafe_destination'
    finally:
        monkeypatch.setattr(os, 'open', real_open)
    assert swapped, 'the contract creates the temporary with os.open relative to the retained folder handle'
    assert os.listdir(canary) == []
    published = os.listdir(moved)  # the documented limit: a held handle may still publish into the renamed folder
    assert published in ([], [vault.target.name])
    if published:
        assert (moved / published[0]).read_bytes() == preview['note'].encode('utf-8')
