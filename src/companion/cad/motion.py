"""Optional timeline for the motion record's driven mate.

Spec keyframes use seconds and mm/degrees; exported slider values use metres,
like the existing motion record. The original parts/mates/driven fields remain
unchanged, so readers without timeline support retain the static driven pose.
"""
import math


def _number(value):
    return type(value) in (int, float) and abs(value) <= 1e9 and math.isfinite(value)


def validate(record, keyframes, *, millimetres=True):
    """Return normal feature reasons, without copying or walking unknown fields."""
    def bad(reason):
        return [{'feature': 'keyframes', 'reason': reason}]
    if not isinstance(keyframes, list) or len(keyframes) > 1000:
        return bad('keyframes must be a list of at most 1000 entries')
    driven = record.get('driven') or {}
    mate = next((m for m in record.get('mates', []) if m.get('id') == driven.get('mate')), None)
    previous = -1
    for frame in keyframes:
        if not isinstance(frame, dict) or set(frame) != {'time', 'mate', 'value'}:
            return bad('each keyframe needs only time, mate and value')
        time, value = frame['time'], frame['value']
        if not _number(time) or not 0 <= time <= 86400 or time <= previous:
            return bad('keyframe times must increase uniquely from zero or later, up to 86400 seconds')
        if mate is None or frame['mate'] != mate['id']:
            return bad('keyframes must name the assembly driven mate')
        if not _number(value):
            return bad('keyframe value must be a finite number')
        scale = 1000 if millimetres and mate['kind'] == 'slider' else 1
        limits = mate.get('range')
        if limits and not limits[0] <= value/scale <= limits[1]:
            return bad('keyframe value is outside the driven mate range')
        from companion.model3d_mates import solve
        result = solve(record, value/scale)
        if not result['ok']:
            return bad(result['reason'])
        previous = time
    return []


def with_keyframes(record, keyframes):
    """Add validated spec keyframes to a metre-based motion export."""
    if keyframes is None:
        return record
    reasons = validate(record, keyframes)
    if reasons:
        raise ValueError(reasons[0]['reason'])
    mate = next((m for m in record['mates'] if m['id'] == record.get('driven', {}).get('mate')), None)
    scale = 1000 if mate and mate['kind'] == 'slider' else 1
    return {**record, 'keyframes': [{**frame, 'value': frame['value']/scale} for frame in keyframes]}


def value_at(record, time):
    """Linearly interpolate exported values, holding the first/last endpoint."""
    if not _number(time):
        raise ValueError('timeline time must be finite')
    frames = record.get('keyframes', [])
    reasons = validate(record, frames, millimetres=False)
    if reasons:
        raise ValueError(reasons[0]['reason'])
    if not frames:
        return record.get('driven', {}).get('value')
    if time <= frames[0]['time']:
        return frames[0]['value']
    for left, right in zip(frames, frames[1:], strict=False):
        if time <= right['time']:
            fraction = (time-left['time'])/(right['time']-left['time'])
            return left['value'] + fraction*(right['value']-left['value'])
    return frames[-1]['value']
