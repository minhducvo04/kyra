"""Recent Studio builds and STEP imports for the headset, read only from the server's own CAD folders."""
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from companion.model3d_glb import part_nodes, read_glb

MAX_RECENT = 20
_ID = re.compile(r"[0-9a-f]{32}")


def glb_path(root: Path, kind: str, ident: str) -> Path:
    return root / "imports" / ident / "model.glb" if kind == "import" else root / ident / "part.glb"


_SKIP = (OSError, ValueError, RecursionError, TypeError, AttributeError, KeyError)


def _sidecar_name(path: Path):
    """The filename recorded beside an import, or None for a missing, linked, unreadable or malformed sidecar."""
    if path.is_symlink():
        return None
    try:
        return json.loads(path.read_text()).get("filename")
    except (*_SKIP, UnicodeDecodeError):
        return None


def _item(root: Path, kind: str, folder: Path) -> dict | None:
    glb = glb_path(root, kind, folder.name)
    if glb.is_symlink():  # never follow a link out of the CAD folders
        return None
    try:
        doc, _ = read_glb(glb.read_bytes())
        parts = part_nodes(doc)
        asset = doc.get("asset")
        extras = asset.get("extras") if isinstance(asset, dict) else None
        extras = extras if isinstance(extras, dict) else {}
        created = datetime.fromtimestamp(folder.stat().st_mtime, UTC).isoformat(timespec="seconds")
    except _SKIP:  # one bad entry is skipped, never fails the list
        return None
    item_kind = "assembly" if kind == "build" and isinstance(extras.get("motion"), dict) else kind
    if kind == "import":
        name = _sidecar_name(folder / "solids.json")
        files = f"/api/cad/imports/{folder.name}"
    else:
        name = extras.get("name")
        files = f"/api/cad/files/{folder.name}"
    return {"id": folder.name, "kind": item_kind, "name": name if isinstance(name, str) and name else f"Build {folder.name[:8]}",
            "created": created,
            "solids": len(parts), "parts": parts, "glb_url": f"{files}.glb", "usdz_url": f"{files}.usdz"}


def recent(root: Path, kind: str) -> list[dict]:
    """Newest first, at most MAX_RECENT; only folders named by a 32-hex id that hold a readable GLB."""
    base = root / "imports" if kind == "import" else root
    if base.is_symlink() or not base.is_dir():
        return []
    folders = sorted((f for f in base.iterdir() if not f.is_symlink() and f.is_dir() and _ID.fullmatch(f.name)),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for folder in folders:
        item = _item(root, kind, folder)
        if item:
            out.append(item)
            if len(out) == MAX_RECENT:
                break
    return out
