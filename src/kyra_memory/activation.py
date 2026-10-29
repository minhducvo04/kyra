"""Pure, relevance-free T12 activation. Times are UTC; durations are days.

Items are mappings with created, usage ({at, who}), links (degree), kind,
weight and optional owner_important/important, superseded and fade_exempt.
Zero-weight exposure is discarded before coalescing and selecting 50 traces:
agent inclusions must neither refresh nor evict a genuine recall.
"""
import math
from datetime import UTC, datetime


def _time(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def components(item, now) -> dict[str, float]:
    """Return B, L, floor and A without reading or mutating any external state."""
    now = _time(now)

    def age(at):
        return max(0.0, (now - _time(at)).total_seconds() / 86400)

    traces = [(age(item['created']), 1.0)]
    for recall in item.get('usage', ()):
        weight = {'owner': 1.0, 'agent': 0.0, 'outcome': 0.5}.get(recall.get('who'), 0.0)
        if weight:
            traces.append((age(recall['at']), weight))
    coalesced = []
    for days, weight in sorted(traces):
        if coalesced and days - coalesced[-1][0] < 0.5:
            coalesced[-1] = (coalesced[-1][0], max(coalesced[-1][1], weight))
        else:
            coalesced.append((days, weight))
    base = math.log(sum(w * (days + 0.01) ** -0.5 for days, w in coalesced[:50]))
    links = min(1.0, 0.3 * math.log1p(max(0, item.get('links', 0))))
    links -= 0.5 * bool(item.get('superseded'))
    protected = (item.get('kind') in ('rule', 'rules', 'pinned') or item.get('owner_important')
                 or item.get('important') or item.get('fade_exempt'))
    floor = 1.0 if protected else 0.6 if item.get('weight', 1.0) > 1.1 else 0.15
    value = floor + (1.0 - floor) / (1.0 + math.exp(-(base + links + 1.4)))
    return {'B': base, 'L': links, 'floor': floor, 'A': value}


def activation(item, now) -> float:
    """A positive ranking multiplier, never a deletion or validity decision."""
    return components(item, now)['A']
