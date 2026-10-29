"""Edge cases for bounded CAD variable arithmetic and resolution."""
from copy import deepcopy

import pytest

from companion.cad.variables import arithmetic, resolve


def _lookup(constants):
    values = dict(constants)

    def lookup(name):
        if name not in values:
            raise ValueError(f'unknown variable {name!r}')
        value = values[name]
        return value if isinstance(value, (int, float)) else arithmetic(value, lookup)

    return lookup


def _part(variables=None, distance='=wall'):
    return {'version': 1, 'units': 'mm', 'variables': variables or {'width': 40, 'wall': 'width / 10'},
            'features': [{'id': 'ex', 'kind': 'extrude', 'sketch': 'sk', 'distance': distance}]}


HOSTILE_EXPRESSIONS = [
    ('__import__("os")', 'arithmetic'),
    ('width.real', 'arithmetic'),
    ('width[0]', 'arithmetic'),
    ('2**10', 'arithmetic'),
    ('lambda: 1', 'arithmetic'),
    ('(' * 500 + '1' + ')' * 500, 'arithmetic'),
    ('-' * 2000 + '1', 'arithmetic'),
    ('1e308 * 10', 'finite'),
    ('1 / 0', 'zero'),
    ('9' * 5000, '512'),
    ('\u0661', 'arithmetic'),  # Arabic-Indic digit one
    ('', 'arithmetic'),
    ('   \t  ', 'arithmetic'),
    ('True', 'arithmetic'),
    ('False', 'arithmetic'),
]


@pytest.mark.parametrize('expression,fragment', HOSTILE_EXPRESSIONS)
def test_arithmetic_refuses_hostile_expression(expression, fragment):
    with pytest.raises(ValueError, match=fragment):
        arithmetic(expression, _lookup({'width': 40}))


def test_arithmetic_refuses_none():
    with pytest.raises(ValueError, match='number or simple arithmetic'):
        arithmetic(None, _lookup({}))


def test_resolve_variable_definition_refuses_none():
    spec = _part({'bad': None})
    _, values, reasons = resolve(spec)
    assert not values
    assert any('bad' in r['reason'] and '512' in r['reason'] for r in reasons)


def test_arithmetic_refuses_nested_lists():
    with pytest.raises(ValueError, match='number or simple arithmetic'):
        arithmetic([['=1']], _lookup({}))


@pytest.mark.parametrize('expression,fragment', HOSTILE_EXPRESSIONS)
def test_resolve_dimension_refuses_hostile_expression(expression, fragment):
    spec = _part(distance=expression)
    _, _, reasons = resolve(spec)
    assert reasons
    assert any(fragment in r['reason'] for r in reasons)


def test_resolve_variable_definition_refuses_nested_list():
    spec = _part({'bad': [['1']]})
    _, values, reasons = resolve(spec)
    assert not values
    assert any('bad' in r['reason'] and '512' in r['reason'] for r in reasons)


def test_self_reference_reports_cycle():
    spec = _part({'x': 'x + 1'})
    _, values, reasons = resolve(spec)
    assert not values
    assert any('cycle' in r['reason'] and 'x' in r['reason'] for r in reasons)


def test_long_cycle_chain_names_every_hop():
    names = [f'v{i}' for i in range(10)]
    variables = {names[i]: names[(i + 1) % len(names)] for i in range(len(names))}
    spec = _part(variables)
    _, values, reasons = resolve(spec)
    assert not values
    reason = next(r['reason'] for r in reasons if 'cycle' in r['reason'])
    for name in names:
        assert name in reason


def test_thousand_variables_fail_closed():
    variables = {f'v{i}': i for i in range(1000)}
    spec = _part(variables)
    original = deepcopy(spec)
    _, values, reasons = resolve(spec)
    assert spec == original
    assert not values
    assert any('100' in r['reason'] and 'keyword' in r['reason'] for r in reasons)


@pytest.mark.parametrize('name', ['if', 'for', 'class', 'lambda', 'return'])
def test_python_keyword_variable_names_fail_closed(name):
    spec = _part({name: 1})
    _, values, reasons = resolve(spec)
    assert not values
    assert any('keyword' in r['reason'] for r in reasons)


def test_overlong_variable_names_fail_closed():
    spec = _part({'a' * 65: 1})
    _, values, reasons = resolve(spec)
    assert not values
    assert any('keyword' in r['reason'] for r in reasons)


def test_deep_unused_field_returns_nesting_reason_not_recursion_error():
    nested = 0
    for _ in range(10_000):
        nested = [nested]
    spec = {'version': 1, 'units': 'mm', 'variables': {'width': 1},
            'features': [{'id': 'ex', 'kind': 'extrude', 'sketch': 'sk', 'distance': 1}],
            'unused': nested}
    unused = spec['unused']
    _, values, reasons = resolve(spec)
    assert spec['unused'] is unused
    assert not values
    assert reasons == [{'feature': '', 'reason': 'spec nesting exceeds 32 levels'}]


def test_resolve_is_deterministic():
    spec = _part()
    first = resolve(deepcopy(spec))
    second = resolve(deepcopy(spec))
    assert first[1] == second[1]
    assert first[2] == second[2]
    assert first[0]['features'][0]['distance'] == second[0]['features'][0]['distance']


def test_resolve_never_mutates_input_spec():
    spec = _part()
    original = deepcopy(spec)
    resolve(spec)
    assert spec == original
