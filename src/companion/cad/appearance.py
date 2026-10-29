"""Optional CAD appearance. Hex colours are sRGB; glTF material factors are linear.

Presets are visual approximations using the mass panel's material names, not
measured optical properties. Density and geometry are unchanged.
"""
import re

from companion.model3d_glb import pack_glb, read_glb

PRESETS = {'steel': (1.0, .35), 'aluminium': (1.0, .45), 'ABS': (0.0, .45), 'PLA': (0.0, .6)}


def valid(value):
    return (isinstance(value, dict) and set(value) == {'colour', 'material'}
            and isinstance(value['colour'], str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value['colour']) is not None
            and isinstance(value['material'], str) and value['material'] in PRESETS)


def material(value):
    def linear(v):
        v /= 255
        return v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
    colour = value['colour']
    metallic, roughness = PRESETS[value['material']]
    return {'name': value['material'], 'extras': {'appearance': dict(value)},
            'pbrMetallicRoughness': {'baseColorFactor': [linear(int(colour[i:i+2], 16)) for i in (1, 3, 5)] + [1],
                                     'metallicFactor': metallic, 'roughnessFactor': roughness}}


def export_materials(path, appearances):
    """Patch only named CAD nodes, preserving mesh buffers, transforms and motion."""
    if not appearances:
        return
    doc, blob = read_glb(path.read_bytes())
    materials = doc.setdefault('materials', [])
    for node in doc.get('nodes', []):
        value = appearances.get(node.get('name'))
        if value is None or 'mesh' not in node:
            continue
        index = len(materials)
        materials.append(material(value))
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            primitive['material'] = index
    path.write_bytes(pack_glb(doc, blob))
