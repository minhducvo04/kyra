"""Design table: named configurations override variables before resolving."""
from copy import deepcopy

import pytest

from companion.cad.build import build
from companion.cad.variables import MAX_CONFIGURATIONS, resolve, resolve_configurations


def part(active=None):
    spec = {'version': 1, 'units': 'mm', 'variables': {'width': 40, 'wall': 'width / 10', 'base': 50},
            'configurations': {'small': {'width': 20}, 'large': {'width': '=base * 2'}},
            'features': [{'id': 'sk', 'kind': 'sketch', 'plane': 'XY', 'entities': [
                {'id': 'r', 'kind': 'rect', 'center': [0, 0], 'width': '=width', 'height': 20}]},
                {'id': 'ex', 'kind': 'extrude', 'sketch': 'sk', 'distance': '=wall', 'mode': 'add'}]}
    if active:
        spec['active_configuration'] = active
    return spec


def test_override_applies_and_base_is_unchanged():
    _, base, reasons = resolve(part())
    assert not reasons and base == {'width': 40, 'wall': 4, 'base': 50}
    out, small, reasons = resolve(part('small'))
    assert not reasons and small == {'width': 20, 'wall': 2, 'base': 50}
    assert out['features'][0]['entities'][0]['width'] == 20
    assert 'configurations' not in out and 'active_configuration' not in out
    _, large, _ = resolve(part('large'))
    assert large['width'] == 100


def test_unknown_configuration_refused():
    _, _, reasons = resolve(part('huge'))
    assert reasons and 'huge' in reasons[0]['reason']
    spec = part()
    spec.pop('configurations')
    spec['active_configuration'] = 'small'
    assert 'small' in resolve(spec)[2][0]['reason']


def test_override_of_missing_variable_refused():
    spec = part('small')
    spec['configurations']['small'] = {'depth_x': 3}
    reasons = resolve(spec)[2]
    assert 'depth_x' in reasons[0]['reason'] and 'small' in reasons[0]['reason']
    rows = resolve_configurations(spec)
    assert [r['ok'] for r in rows] == [False, False]  # a bad table refuses every row


def test_cycle_through_override_refused():
    spec = part('small')
    spec['configurations']['small'] = {'width': 'wall + 1'}
    reasons = resolve(spec)[2]
    assert any('cycle' in r['reason'] for r in reasons)


def test_too_many_configurations_refused():
    spec = part('c0')
    spec['configurations'] = {f'c{i}': {'width': i} for i in range(MAX_CONFIGURATIONS + 1)}
    assert 'at most' in resolve(spec)[2][0]['reason']
    assert 'at most' in resolve_configurations(spec)[0]['reasons'][0]['reason']
    assert MAX_CONFIGURATIONS == 100


@pytest.mark.parametrize('bad', [[], 'x', {'a': 5}, {'': {}}, {'a': {1: 2}}])
def test_malformed_table_gives_reason_not_exception(bad):
    spec = part()
    spec['configurations'] = bad
    spec['active_configuration'] = 'a'
    assert resolve(spec)[2]
    resolve_configurations(spec)


def test_deep_override_value_refused_without_recursion_error():
    deep = '1'
    for _ in range(10000):
        deep = [deep]
    spec = part('small')
    spec['configurations']['small'] = {'width': deep}
    assert 'nesting' in resolve(spec)[2][0]['reason']
    assert all(not r['ok'] for r in resolve_configurations(spec))
    deep_expr = '(' * 10000 + '1' + ')' * 10000
    spec['configurations']['small'] = {'width': deep_expr}
    assert resolve(spec)[2]


def test_check_all_rows_deterministic_and_input_not_mutated():
    spec = part('small')
    before = deepcopy(spec)
    rows = resolve_configurations(spec)
    assert spec == before
    assert rows == resolve_configurations(spec)
    assert [(r['name'], r['ok'], r['variables']['width']) for r in rows] == [('small', True, 20), ('large', True, 100)]
    assert resolve(spec) == resolve(deepcopy(spec))
    assert spec == before


def test_real_kernel_two_configurations_differ(tmp_path):
    boxes = {}
    for name in ('small', 'large'):
        body = build(part(name), tmp_path)
        assert body['ok'], body
        boxes[name] = body['bbox_mm']
    assert boxes['small'] != boxes['large']
    assert build(part(), tmp_path)['bbox_mm'] not in (boxes['small'], boxes['large'])
    print(boxes)


def test_hundred_configurations_accepted_hundred_and_one_refused():
    spec = part('c0')
    spec['configurations'] = {f'c{i}': {'width': i + 1} for i in range(100)}
    assert not resolve(spec)[2]
    assert len(resolve_configurations(spec)) == 100
    spec['configurations']['c100'] = {'width': 5}
    assert 'at most 100' in resolve(spec)[2][0]['reason']
    assert 'at most 100' in resolve_configurations(spec)[0]['reasons'][0]['reason']


@pytest.mark.parametrize('bad', [[], 'broken', 7])
def test_check_all_reports_invalid_table_type(bad):
    spec = part()
    spec['configurations'] = bad
    rows = resolve_configurations(spec)
    assert len(rows) == 1 and rows[0]['ok'] is False and rows[0]['reasons'][0]['reason']
    assert resolve_configurations(part().__class__(version=1)) == []
