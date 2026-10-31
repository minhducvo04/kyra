import pytest

from companion import team_chat
from tests.test_team_chat_acceptance import BUILD, PASS, FakeTransport, bound, respond

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")


def test_direct_worker_retains_review_and_identity(tmp_path):
    transport = FakeTransport()
    store, service = bound(team_chat, tmp_path, transport)
    room = store.create_room('Direct task')['id']
    result = store.submit(room, 'Fix the fixture and verify it.', 'direct', execution_mode='worker')
    run = result['run_id']
    assert store.submit(room, 'Fix the fixture and verify it.', 'direct', execution_mode='worker') == result
    with pytest.raises(ValueError):
        store.submit(room, 'Fix the fixture and verify it.', 'direct', execution_mode='plan')
    service.step(run)
    assert transport.stages() == [('codex', 'build')]
    respond(transport, 'build', **BUILD)
    service.step(run)
    service.step(run)
    assert transport.stages() == [('codex', 'build'), ('claude', 'review')]
    respond(transport, 'review', **PASS)
    service.step(run)
    assert store.get_room(room)['runs'][0]['status'] == 'completed'


def test_direct_never_leaks_private_room_or_truncates_task(tmp_path):
    store, _ = bound(team_chat, tmp_path, FakeTransport())
    room = store.create_room('Private', local_only=True)['id']
    with pytest.raises(ValueError):
        store.submit(room, 'A task', 'x', execution_mode='worker')
    normal = store.create_room('Normal')['id']
    with pytest.raises(ValueError):
        store.submit(normal, 'x' * 8001, 'long', execution_mode='worker')
    assert store.get_room(normal)['messages'] == []
