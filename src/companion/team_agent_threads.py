"""Agent-thread routes for the Team page (T1): a thread per delegation child row, read and answered by key.

GET  /api/team/rooms/{room}/agent-threads                                   {threads: {'<request>/<key>': {thread_id, count, unread}}}
GET  /api/team/rooms/{room}/delegations/{request}/children/{key}/thread     {thread_id, member, lines}
POST /api/team/rooms/{room}/delegations/{request}/children/{key}/reply      the owner's follow-up to that same member
Delivery is team_replies.pump on the Team wake loop; nothing here sends, builds or dispatches.
"""
from fastapi import APIRouter, Depends

from companion.errors import ApiError
from companion.settings import get_settings
from companion.team_http import ReplyInput, action, get_service, get_store, wake_service
from companion.team_threads import Store, agent_thread_id, child_member, delegation_row

router = APIRouter()


def _child(store, room_id, request_id, key):
    row = delegation_row(store.root, room_id, request_id)
    member = row and key in row.get('children', {}) and child_member(row, key)
    if not member:
        raise ApiError(404, 'not_found', 'That step has no agent thread.')
    return member


@router.get('/api/team/rooms/{room_id}/agent-threads')
def agent_threads(room_id: str, store=Depends(get_store)):
    action(store.get_room, room_id)
    return {'threads': Store(store.root).agent_threads(room_id)}


@router.get('/api/team/rooms/{room_id}/delegations/{request_id}/children/{key}/thread')
def agent_thread(room_id: str, request_id: str, key: str, store=Depends(get_store)):
    action(store.get_room, room_id)
    member = _child(store, room_id, request_id, key)
    thread_id = agent_thread_id(room_id, request_id, key)
    return {'thread_id': thread_id, 'member': member, 'lines': Store(store.root).agent_lines(thread_id)}


@router.post('/api/team/rooms/{room_id}/delegations/{request_id}/children/{key}/reply')
def agent_reply(room_id: str, request_id: str, key: str, body: ReplyInput, store=Depends(get_store),
                service=Depends(get_service)):
    room = action(store.get_room, room_id)
    member = _child(store, room_id, request_id, key)
    try:
        result = action(Store(store.root).agent_reply, room, request_id, key, member, body.text, body.request_id)
    except PermissionError:
        raise ApiError(403, 'local_only_room', 'This chat stays local. Agent threads exist only in shared chats.') from None
    if get_settings().inline_worker:
        wake_service(service)
    return result
