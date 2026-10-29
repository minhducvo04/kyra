"""Bounded arithmetic resolution, without executing source or mutating the spec.

A part may also carry `configurations` ({name: {variable: number or expression}}) and
`active_configuration`; the active row overrides `variables` before evaluation.
Part variables are a mapping of identifier to number or expression. Dimension strings
may start with '='. Only numbers, names, parentheses and + - * / are accepted.
"""
import ast
import keyword
import math
import operator
import re
from copy import deepcopy

NUMBER = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
DIMENSIONS = {'offset', 'width', 'height', 'radius', 'diameter', 'distance', 'depth',
              'angle', 'thickness', 'count', 'center', 'start', 'end', 'start_deg',
              'end_deg', 'spacing', 'axis', 'value', 'at'}
MAX_CONFIGURATIONS = 100
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def arithmetic(source, lookup):
    """Parse a small expression; reject all other Python syntax before visiting it."""
    if type(source) in (int, float):
        try:
            if not math.isfinite(source):
                raise ValueError('equation must produce a finite number')
        except OverflowError:
            raise ValueError('equation exceeds arithmetic limits') from None
        return source
    if not isinstance(source, str) or len(source) > 512:
        raise ValueError('use a number or simple arithmetic expression (up to 512 characters)')
    source = source.strip().removeprefix('=').strip()
    tokens = re.findall(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[A-Za-z_][A-Za-z0-9_]*|[()+*/-]', source)
    if ''.join(tokens) != re.sub(r'\s', '', source):
        raise ValueError('use simple arithmetic: numbers, names, + - * / and parentheses')
    try:
        tree = ast.parse(source, mode='eval')
    except (SyntaxError, ValueError, RecursionError):
        raise ValueError('use simple arithmetic: numbers, names, + - * / and parentheses') from None
    nodes = list(ast.walk(tree))
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
    if (len(nodes) > 128 or any(not isinstance(n, allowed) for n in nodes)
            or any(isinstance(n, ast.Constant) and not NUMBER.fullmatch(ast.get_source_segment(source, n) or '')
                   for n in nodes)):
        raise ValueError('use simple arithmetic: numbers, names, + - * / and parentheses')

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.Name):
            value = lookup(node.id)
        elif isinstance(node, ast.BinOp):
            value = OPS[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError('use simple arithmetic with numeric constants only')
        if not math.isfinite(value):
            raise ValueError('equation must produce a finite number')
        return value
    try:
        value = visit(tree.body)
        return int(value) if value == int(value) else value
    except ZeroDivisionError:
        raise ValueError('division by zero in equation') from None
    except (OverflowError, RecursionError):
        raise ValueError('equation exceeds arithmetic limits') from None


def _configuration_problem(configs, active, definitions):
    """Return a reason when the design table is malformed, else ''."""
    if configs is None:
        return f'unknown configuration {active!r}' if isinstance(active, str) else 'active_configuration must be a name'
    if not isinstance(configs, dict):
        return 'configurations must be a mapping of name to variable overrides'
    if len(configs) > MAX_CONFIGURATIONS:
        return f'at most {MAX_CONFIGURATIONS} configurations'
    for name, row in configs.items():
        if not isinstance(name, str) or not 0 < len(name) <= 64:
            return 'configuration names are text of 1 to 64 characters'
        if not isinstance(row, dict):
            return f'configuration {name!r} must map variables to numbers or expressions'
        for var in row:
            if not isinstance(var, str) or var not in definitions:
                return f'configuration {name!r} overrides undefined variable {var!r}'
    if active not in (None, '') and (not isinstance(active, str) or active not in configs):
        return f'unknown configuration {active!r}'
    return ''


def resolve_configurations(spec):
    """Resolve every configuration without building; for a "Check all" button.

    Returns [{'name', 'ok', 'variables', 'reasons'}], one row per configuration in order.
    The input is never changed. A malformed table gives one row with the reason."""
    configs = spec.get('configurations') if isinstance(spec, dict) else None
    if not isinstance(spec, dict):
        return []
    if configs is None or (isinstance(configs, dict) and not configs):
        return []
    problem = ''
    if not isinstance(spec, dict):
        problem = 'spec must be a mapping'
    elif not isinstance(configs, dict):
        problem = 'configurations must be a mapping of name to variable overrides'
    elif len(configs) > MAX_CONFIGURATIONS:
        problem = f'at most {MAX_CONFIGURATIONS} configurations'
    if problem:
        return [{'name': '', 'ok': False, 'variables': {}, 'reasons': [{'feature': '', 'reason': problem}]}]
    rows = []
    for name in configs:
        _, values, reasons = resolve({**spec, 'active_configuration': name})
        rows.append({'name': name, 'ok': not reasons, 'variables': values, 'reasons': reasons})
    return rows


def resolve(spec):
    """Return (resolved copy, variable values, reasons naming active features)."""
    # Check untrusted containers iteratively before deepcopy or dimension recursion.
    # Ordinary feature/entity/coordinate nesting is well below this limit.
    pending = [(spec, 0, '', False)]
    while pending:
        item, depth, feature, feature_list = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        if depth > 32:
            return spec, {}, [{'feature': feature, 'reason': 'spec nesting exceeds 32 levels'}]
        if isinstance(item, dict):
            pending.extend((child, depth + 1, feature, item is spec and key == 'features')
                           for key, child in item.items())
        else:
            for child in item:
                fid = child.get('id', '') if feature_list and isinstance(child, dict) else feature
                pending.append((child, depth + 1, fid if isinstance(fid, str) else '', False))
    result, values, errors, visiting = deepcopy(spec), {}, {}, []
    if not isinstance(spec, dict) or spec.get('kind') == 'assembly':
        return result, values, []
    definitions = spec.get('variables', {})
    if (not isinstance(definitions, dict) or len(definitions) > 100
            or any(not isinstance(n, str) or not NAME.fullmatch(n) or keyword.iskeyword(n) for n in definitions)):
        return result, values, [{'feature': '', 'reason': 'variables need at most 100 non-keyword names (letters, digits, underscore; no leading digit)'}]

    overrides = {}
    configs = spec.get('configurations')
    active_name = spec.get('active_configuration')
    if configs is not None or active_name not in (None, ''):
        problem = _configuration_problem(configs, active_name, definitions)
        if problem:
            return result, values, [{'feature': '', 'reason': problem}]
        if active_name not in (None, ''):
            overrides = configs[active_name]
            definitions = {**definitions, **overrides}

    def lookup(name):
        if name in values:
            return values[name]
        if name in errors:
            raise ValueError(errors[name])
        if name not in definitions:
            raise ValueError(f'unknown variable {name!r}')
        if name in visiting:
            raise ValueError('variable cycle: ' + ' -> '.join([*visiting, name]))
        visiting.append(name)
        try:
            values[name] = arithmetic(definitions[name], lookup)
            return values[name]
        except ValueError as exc:
            errors[name] = f'{name}: {exc}'
            raise ValueError(errors[name]) from None
        finally:
            visiting.pop()

    reasons = []
    for name in definitions:
        try:
            lookup(name)
        except ValueError as exc:
            reasons.append({'feature': '', 'reason': str(exc)})

    def dimension(value):
        if isinstance(value, list):
            return [dimension(v) for v in value]
        return arithmetic(value, lookup) if isinstance(value, str) else value

    def dimensions(item):
        if not isinstance(item, dict):
            return
        for key in DIMENSIONS & item.keys():
            if ((key == 'depth' and item[key] == 'through')
                    or (key == 'axis' and isinstance(item[key], str) and item[key] in {'X', 'Y'})):
                continue
            item[key] = dimension(item[key])
        for key in ('entities', 'constraints'):
            if isinstance(item.get(key), list):
                for child in item[key]:
                    dimensions(child)

    features = result.get('features')
    if isinstance(features, list):
        rollback = result.get('rollback', len(features))
        active = features[:rollback] if type(rollback) is int and 0 <= rollback <= len(features) else []
        for feature in active:
            if not isinstance(feature, dict) or feature.get('suppressed'):
                continue
            try:
                dimensions(feature)
            except ValueError as exc:
                reasons.append({'feature': feature.get('id', ''), 'reason': str(exc)})
    # Resolved specs must not re-evaluate definitions on subsequent validation.
    result.pop('variables', None)
    result.pop('configurations', None)
    result.pop('active_configuration', None)
    return result, values, reasons
