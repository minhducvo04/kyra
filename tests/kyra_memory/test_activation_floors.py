"""Tests for components() in src/kyra_memory/activation.py."""
from datetime import UTC, datetime, timedelta

import pytest

from kyra_memory.activation import activation, components

NOW = datetime(2026, 10, 26, tzinfo=UTC)

def stamp(days):
    return (NOW - timedelta(days=days)).isoformat()

def test_floors():
    # floor is 1.0 for kind 'rule', 'pinned' or fade_exempt
    item_rule = {'created': stamp(10), 'kind': 'rule'}
    assert components(item_rule, NOW)['floor'] == 1.0
    assert activation(item_rule, NOW) == 1.0

    item_pinned = {'created': stamp(10), 'kind': 'pinned'}
    assert components(item_pinned, NOW)['floor'] == 1.0
    assert activation(item_pinned, NOW) == 1.0

    item_fade_exempt = {'created': stamp(10), 'fade_exempt': True}
    assert components(item_fade_exempt, NOW)['floor'] == 1.0
    assert activation(item_fade_exempt, NOW) == 1.0

    item_owner_important = {'created': stamp(10), 'owner_important': True}
    assert components(item_owner_important, NOW)['floor'] == 1.0
    assert activation(item_owner_important, NOW) == 1.0

    # floor is 0.6 when weight is above 1.1
    item_heavy = {'created': stamp(10), 'weight': 1.2}
    assert components(item_heavy, NOW)['floor'] == 0.6

    # else 0.15
    item_light = {'created': stamp(10), 'weight': 1.0}
    assert components(item_light, NOW)['floor'] == 0.15

def test_a_bounds():
    # A is never below floor and never above 1.0
    for floor in [0.15, 0.6, 1.0]:
        item = {'created': stamp(10), 'weight': 1.0}
        if floor == 0.6:
            item['weight'] = 1.2
        elif floor == 1.0:
            item['kind'] = 'rule'
        
        comp = components(item, NOW)
        assert comp['floor'] <= comp['A'] <= 1.0

def test_superseded_links_reduction():
    # a superseded item has links lower by 0.5 than the same item not superseded
    item_normal = {'created': stamp(10), 'links': 10}
    item_superseded = {'created': stamp(10), 'links': 10, 'superseded': True}
    
    l_normal = components(item_normal, NOW)['L']
    l_superseded = components(item_superseded, NOW)['L']
    
    assert pytest.approx(l_superseded - l_normal) == -0.5

def test_links_cap():
    # links term is capped at 1.0
    item_huge_links = {'created': stamp(10), 'links': 1000}
    assert components(item_huge_links, NOW)['L'] == 1.0

def test_usage_filtering():
    # usage entries with who='agent' or an unknown who add no trace, so A equals the no-usage value
    item_base = {'created': stamp(10)}
    base_a = activation(item_base, NOW)
    
    item_agent = {**item_base, 'usage': [{'at': stamp(1), 'who': 'agent'}]}
    assert activation(item_agent, NOW) == base_a
    
    item_unknown = {**item_base, 'usage': [{'at': stamp(1), 'who': 'stranger'}]}
    assert activation(item_unknown, NOW) == base_a

def test_coalescing():
    # two owner recalls less than 0.5 day apart coalesce into one trace
    item = {'created': stamp(10)}
    # Two owner recalls at 1.0 and 1.4 days ago
    usage = [
        {'at': stamp(1.0), 'who': 'owner'},
        {'at': stamp(1.4), 'who': 'owner'}
    ]
    # If they didn't coalesce, they'd be two traces. 
    # If they do, they are one trace with max weight.
    # We can check this by seeing if the result matches a single trace with the same weight.
    single_usage = [{'at': stamp(1.0), 'who': 'owner'}]
    
    # Note: the logic for coalescing is in components()
    # We can't easily check the number of traces directly from activation, 
    # but we can check if the result matches a single trace with the same weight.
    # Since both are 'owner', weight is 1.0.
    assert components({**item, 'usage': usage}, NOW) == components({**item, 'usage': single_usage}, NOW)

def test_datetime_types():
    # naive datetimes and ISO strings are read as UTC and give the same result
    item_iso = {'created': stamp(10)}
    item_dt = {'created': datetime.fromisoformat(stamp(10)).replace(tzinfo=None)}
    
    assert components(item_iso, NOW) == components(item_dt, NOW)
