"""Read canonical bodies produced by the isolated STEP import worker.

Never read source.step here. Imports created before canonical bodies were stored
must be uploaded again. Archive directories are deliberately not searched.
"""
import re
from pathlib import Path


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def load(root, import_id):
    if root is None or not valid_id(import_id):
        raise ValueError('Import is unknown or archived.')
    imports = Path(root) / 'imports'
    folder = imports / import_id
    body, metadata = folder / 'body.brep', folder / 'solids.json'
    if any(p.is_symlink() for p in (Path(root), imports, folder, body, metadata)):
        raise ValueError('Import symlinks are not allowed.')
    if not folder.is_dir() or not metadata.is_file():
        raise ValueError('Import is unknown or archived.')
    if not body.is_file():
        raise ValueError('Import has no stored base body. Upload the STEP file again.')
    from build123d import import_brep
    try:
        shape = import_brep(body)
        if shape is None or not shape.is_valid or shape.volume <= 0 or not shape.solids():
            raise ValueError('empty body')
        return shape
    except Exception:
        raise ValueError('Import has no valid stored base body. Upload the STEP file again.') from None
