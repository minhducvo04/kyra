"""Assembly BOM in first-instance order, independent of pose and mates.

Saved IDs define stock identity; inline specs group by canonical JSON (including
name/material). Part-spec metadata: material (text), density_kg_m3 (0 < n <= 100000).
Mass is per unit and total, in kg. Unknown density stays null, never guessed.
"""
import csv
import io
import json
import math

from companion.cad import parts
from companion.cad.spec import validate
from companion.cad.variables import resolve
from companion.errors import ApiError


def bill_of_materials(spec, root):
    if not isinstance(spec, dict) or spec.get('kind') != 'assembly':
        return {'ok': False, 'rows': [], 'reasons': [{'feature': '', 'reason': 'Send an assembly spec.'}]}
    try:
        _, _, reasons = resolve(spec)
        if not reasons:
            reasons = validate(spec)
    except (TypeError, ValueError, OverflowError, RecursionError):
        # Legacy validators assume some field types; contain malformed request
        # values here without swallowing unrelated kernel/programming failures.
        reasons = [{'feature': '', 'reason': 'Assembly contains malformed values or out-of-range numbers.'}]
    if reasons:
        return {'ok': False, 'rows': [], 'reasons': reasons}
    groups = {}
    for instance in spec['parts']:
        ref = instance.get('part_ref')
        key = ('ref', ref) if ref else ('inline', json.dumps(instance['part'], sort_keys=True))
        if key in groups:
            groups[key]['quantity'] += 1
            continue
        name = instance.get('part', {}).get('name')
        name = name if isinstance(name, str) and name else instance['id']
        row = {'item': len(groups) + 1, 'part_name': ref or name,
               'part_ref': ref, 'inline': ref is None, 'quantity': 1,
               'material': None, 'mass_kg': None, 'total_mass_kg': None, 'reasons': []}
        groups[key] = row

        def refuse(message, row=row, instance=instance):
            row['reasons'].append({'feature': instance['id'], 'reason': message})

        try:
            part = instance.get('part')
            if ref:
                record = parts.read(root, ref)
                part = record['spec']
                if not isinstance(record['name'], str):
                    raise ValueError('invalid saved name')
                row['part_name'] = record['name']
            errors = validate(part)
            if errors or part.get('kind') == 'assembly':
                refuse('Invalid part: ' + (errors[0]['reason'] if errors else 'nested assembly is unsupported'))
                continue
        except (ApiError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            refuse(f'Saved part {ref} is missing, archived or unreadable.')
            continue
        material, density = part.get('material'), part.get('density_kg_m3')
        if material is not None and (not isinstance(material, str) or len(material) > 200):
            refuse('material must be text up to 200 characters.')
            continue
        row['material'] = material
        if density is None:
            continue
        if type(density) not in (int, float) or not 0 < density <= 100000 or not math.isfinite(density):
            refuse('density_kg_m3 must be greater than 0 and at most 100000.')
            continue
        try:
            from companion.cad.build import _solid
            solid, _, _, errors = _solid(part, root=root)
            if errors:
                refuse('Cannot calculate mass: ' + errors[0]['reason'])
            else:
                mass = solid.volume * density / 1e9
                if not math.isfinite(mass) or mass <= 0:
                    refuse('Cannot calculate a positive finite mass.')
                else:
                    row['mass_kg'] = mass
        except ImportError:
            refuse('CAD kernel unavailable; cannot calculate mass.')
    rows = list(groups.values())
    for row in rows:
        if row['mass_kg'] is not None:
            row['total_mass_kg'] = row['mass_kg'] * row['quantity']
    reasons = [reason for row in rows for reason in row['reasons']]
    return {'ok': not reasons, 'rows': rows, 'reasons': reasons}


def csv_text(rows):
    """Successful BOM only; quote spreadsheet formula prefixes in display text."""
    stream = io.StringIO(newline='')
    fields = ['item', 'part_name', 'part_ref', 'inline', 'quantity', 'material', 'mass_kg', 'total_mass_kg']
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        for field in ('part_name', 'material'):
            value = safe[field]
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                safe[field] = "'" + value
        writer.writerow(safe)
    return stream.getvalue()
