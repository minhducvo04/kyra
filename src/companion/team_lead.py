"""Private local answers in the canonical Team transcript; no native authority."""
import hashlib
import json
import time
import uuid
from copy import deepcopy

from companion.local_lead_inference import LeadBusyError, LeadInferenceError
from companion.team_chat import (
    ANSWER_ACTIVE,
    ANSWER_NOTICES,
    ANSWER_ROUTES,
    TeamCapacityError,
    _hex,
    _text,
    local_answers,
    require_admission,
    validate_answer,
    validate_lead_binding,
)

__all__ = ['LeadService', 'LeadBusyError']


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate field')
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError('Invalid constant')


def _validated_result(result, capsule, binding):
    """Validate the trusted worker envelope, then its untrusted generated JSON."""
    keys = {'project', 'room', 'request_id', 'capsule_sha256', 'attempt_id', 'text',
            'included_source_ids', 'included_history_ids', 'omitted_counts', 'truncated',
            'effective_input_sha256', 'model', 'metrics'}
    if not isinstance(result, dict) or set(result) != keys or len(_encoded(result)) > 32 * 1024:
        raise ValueError('Invalid local output')
    if any(result[k] != capsule[k] for k in ('project', 'room', 'request_id', 'capsule_sha256')):
        raise ValueError('Invalid local output identity')
    if not _hex(result['attempt_id']) or result['model'] != {'id': binding['model_id']}:
        raise ValueError('Invalid local output model')
    metrics = result['metrics']
    if (not isinstance(metrics, dict) or set(metrics) != {'prompt_tokens', 'generation_tokens', 'finish_reason'}
            or type(metrics['prompt_tokens']) is not int or not 1 <= metrics['prompt_tokens'] <= 6000
            or type(metrics['generation_tokens']) is not int or not 1 <= metrics['generation_tokens'] <= 512
            or metrics['finish_reason'] != 'stop'):
        raise ValueError('Invalid local output metrics')
    effective = {k: v for k, v in capsule.items() if k != 'capsule_sha256'}
    omissions = {}
    for group, field in (('sources', 'included_source_ids'), ('history', 'included_history_ids')):
        offered = capsule[group]
        ids = result[field]
        if (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or len(ids) > len(offered)):
            raise ValueError('Invalid included context')
        retained = offered[:len(ids)] if group == 'sources' else (offered[-len(ids):] if ids else [])
        if ids != [item['id'] for item in retained]:
            raise ValueError('Invalid included context order')
        effective[group] = retained
        omissions[group] = len(offered) - len(retained)
    counts = result['omitted_counts']
    if (not isinstance(counts, dict) or set(counts) != {'sources', 'history'}
            or any(type(v) is not int for v in counts.values()) or counts != omissions
            or type(result['truncated']) is not bool or result['truncated'] != any(omissions.values())
            or result['effective_input_sha256'] != _digest(effective)):
        raise ValueError('Invalid local output provenance')
    text = result['text']
    if not isinstance(text, str):
        raise ValueError('Invalid generated JSON')
    output = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
    if (not isinstance(output, dict) or set(output) != {'answer', 'sources', 'missing', 'route'}
            or type(output['missing']) is not bool or output['route'] not in ANSWER_ROUTES
            or not isinstance(output['sources'], list) or len(output['sources']) > 8
            or any(not isinstance(i, str) for i in output['sources'])
            or len(output['sources']) != len(set(output['sources']))
            or any(i not in result['included_source_ids'] for i in output['sources'])):
        raise ValueError('Invalid generated answer')
    answer = _text(output['answer'], 4000)
    by_id = {s['id']: s for s in binding['sources']}
    return dict(text=answer, sources=[deepcopy(by_id[i]) for i in output['sources']],
                route=output['route'], missing=output['missing'], omitted_counts=deepcopy(counts))


class _StopSignal:
    def __init__(self, store, room_id, request_id, attempt_id):
        self.store, self.room_id, self.request_id, self.attempt_id = store, room_id, request_id, attempt_id

    def is_set(self):
        try:
            answer = self.store.get_answer(self.room_id, self.request_id)
            return (answer['stop_requested'] or answer['status'] != 'running'
                    or answer['attempt_id'] != self.attempt_id)
        except (OSError, ValueError, LookupError):
            return True


def has_pending_answers(store):
    """Observe valid saved obligations without creating state or checking admission."""
    data = store._read()
    store.check_namespace(data)
    answers = [answer for _, answer in local_answers(data)]
    for answer in answers:
        validate_answer(answer)
    return any(answer['status'] in ANSWER_ACTIVE for answer in answers)


class LeadService:
    def __init__(self, store, backend, *, project, source_reader, monotonic=time.monotonic, context_provider=None):
        validate_lead_binding(project)
        self.store, self.backend = store, backend
        self.project = deepcopy(project)
        self.source_reader, self.monotonic = source_reader, monotonic
        self.context_provider = context_provider

    def submit(self, room_id, text, request_id):
        # A duplicate remains a duplicate even if the runtime is now unavailable.
        try:
            self.store.get_answer(room_id, request_id)
        except LookupError:
            available = self.backend.availability()
            if not available['available']:
                reason = available.get('reason', 'cache_unavailable')
                if reason == 'busy':
                    raise LeadBusyError() from None
                raise LeadInferenceError(reason if reason in ANSWER_NOTICES - {'', 'source_changed'} else 'cache_unavailable') from None
        return self.store.queue_answer(room_id, text, request_id, binding=self.project)

    def stop(self, room_id, request_id):
        return self.store.request_answer_stop(room_id, request_id)

    def status(self):
        result = dict(available=False, active=0, queued=0, reason='cache_unavailable')
        try:
            data = self.store._read()
            self.store.check_namespace(data)
            answers = [answer for _, answer in local_answers(data)]
            for answer in answers:
                validate_answer(answer)
            result.update(active=sum(a['status'] == 'running' for a in answers),
                          queued=sum(a['status'] == 'queued' for a in answers))
            registry = self.store.controller.read_existing_claims()
            require_admission(data, data, claims=registry['claims'] if registry else [])
            available = self.backend.availability()
            reason = available.get('reason', '')
            result.update(available=available.get('available') is True,
                          reason=reason if reason in ANSWER_NOTICES | {'busy'} else 'cache_unavailable')
        except TeamCapacityError:
            result.update(available=False, reason='team_capacity')
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            result.update(available=False, reason='cache_unavailable')
        return result

    def _capsule(self, room_id, room, answer):
        if answer['binding'] != self.project:
            raise LeadInferenceError('model_mismatch')
        parent = next((m for m in room['messages'] if m['id'] == answer['reply_to'] and m['author'] in {'user', 'system'}), None)
        if parent is None:
            raise LeadInferenceError('output_invalid')
        sources, excluded = [], 0
        raw_size = 0
        for source in answer['binding']['sources']:
            try:
                text = self.source_reader(source['ref'], source['sha256'])
            except (ValueError, OSError):
                excluded += 1
                continue
            if not isinstance(text, str):
                raise LeadInferenceError('output_invalid')
            # Bound individual reads already returned by the trusted resolver before building a capsule.
            raw_size += len(text.encode('utf-8'))
            if raw_size > 128 * 1024:
                raise LeadInferenceError('input_too_large')
            sources.append({**source, 'text': text})
        history = []
        for message in room['messages']:
            if message['id'] == parent['id']:
                break
            role = None
            if message.get('author') == 'user' and message.get('kind') is None:
                role = 'user'
            elif message.get('kind') == 'answer' and message.get('status') == 'answered':
                validate_answer(message)
                role = 'assistant'
            if role is not None:
                history.append(dict(id=message['id'], role=role, text=message['text']))
        if len(history) > 500 or len({h['id'] for h in history}) != len(history):
            raise LeadInferenceError('input_too_large')
        capsule = dict(request=parent['text'], sources=sources, history=history,
                       project=self.project['project_id'], room=room_id, request_id=answer['request_id'])
        if self.context_provider is not None:
            context = self.context_provider(parent['text'])
            if not isinstance(context, dict) or set(context) != {'capability_context', 'usage_context'}:
                raise LeadInferenceError('output_invalid')
            for key, limit in (('capability_context', 8192), ('usage_context', 2048)):
                if not isinstance(context[key], str) or len(context[key].encode('utf-8')) > limit:
                    raise LeadInferenceError('input_too_large')
            capsule.update(context)
        capsule['capsule_sha256'] = _digest(capsule)
        if len(_encoded(capsule)) > 128 * 1024:
            raise LeadInferenceError('input_too_large')
        return capsule, excluded

    @staticmethod
    def _settle(answer, *, status, notice, output=None):
        answer.update(status=status, notice=notice, finished_at=time.time(), text='', sources=[],
                      route=None, missing=None)
        if output is not None:
            answer.update(output)
        validate_answer(answer)

    def run_once(self):
        # Avoid creating even the adapter lock when there is no local obligation.
        if not has_pending_answers(self.store):
            return False
        try:
            with self.backend.flight() as flight:
                with self.store.edit() as data:
                    self.store.check_namespace(data)
                    recovered = False
                    for room_id, answer in local_answers(data):
                        validate_answer(answer)
                        if answer['status'] == 'running':
                            stopped = answer['stop_requested']
                            self._settle(answer, status='stopped' if stopped else 'failed',
                                         notice='cancelled' if stopped else 'worker_failed')
                            data['rooms'][room_id]['room']['updated_at'] = time.time()
                            recovered = True
                    queued = [(rid, a) for rid, a in local_answers(data) if a['status'] == 'queued']
                    if not queued:
                        return recovered
                    room_id, answer = min(queued, key=lambda item: (item[1]['created_at'], item[1]['id']))
                    answer.update(status='running', started_at=time.time(), attempt_id=uuid.uuid4().hex)
                    validate_answer(answer)
                    room = deepcopy(data['rooms'][room_id])
                    snapshot = deepcopy(answer)
                signal = _StopSignal(self.store, room_id, snapshot['request_id'], snapshot['attempt_id'])
                output, notice, excluded = None, '', 0
                try:
                    capsule, excluded = self._capsule(room_id, room, snapshot)
                    if signal.is_set():
                        raise LeadInferenceError('cancelled')
                    result = flight.generate(capsule, cancel_event=signal)
                    output = _validated_result(result, capsule, snapshot['binding'])
                    output['omitted_counts']['sources'] += excluded
                    notice = 'source_changed' if excluded else ''
                except LeadInferenceError as exc:
                    notice = exc.code if exc.code in ANSWER_NOTICES else 'worker_failed'
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                    notice = 'output_invalid'
                except Exception:
                    notice = 'worker_failed'
                # generate returns/raises only after its child has exited. The flight
                # stays held through this save; Stop accepted first beats any output.
                with self.store.edit() as data:
                    self.store.check_namespace(data)
                    room = data['rooms'].get(room_id)
                    if room is None:
                        raise ValueError('Local answer disappeared')
                    answer = next((m for m in room['messages'] if m['id'] == snapshot['id']), None)
                    if (answer is None or answer['status'] != 'running'
                            or answer['attempt_id'] != snapshot['attempt_id']):
                        raise ValueError('Local answer changed')
                    if answer['stop_requested']:
                        self._settle(answer, status='stopped', notice='cancelled')
                    elif output is None:
                        self._settle(answer, status='failed', notice=notice or 'worker_failed')
                    else:
                        self._settle(answer, status='answered', notice=notice, output=output)
                    room['room']['updated_at'] = time.time()
                return True
        except LeadBusyError:
            return False
