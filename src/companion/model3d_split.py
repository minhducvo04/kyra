"""Automatic parts for a real generator's single fused mesh, so "see inside" works honestly.

A photo-to-3D model (TripoSR and friends) returns one fused shell with no parts. To take it apart we split it
by geometry only, never by meaning:
  (a) connected components, when the mesh has two or more pieces of real size (each at least 1% of the faces);
  (b) otherwise N horizontal layers (default 5) along the up axis (+y, glTF's convention), each triangle going to
      the layer that holds its centroid, labelled "Layer i (automatic slice)" and exploding up or down.
The job records `parts_kind` ("named" | "components" | "slices") so a viewer can say the parts are automatic,
not anatomical. Vertex colours and normals are kept. Pure python on the GLB data.
"""
from pathlib import Path

from companion.model3d_glb import Mesh, Part, build_glb, read_glb, read_mesh

DEFAULT_LAYERS = 5
MIN_COMPONENT_SHARE = 0.01
MAX_COMPONENTS = 12
WHITE = (1.0, 1.0, 1.0, 1.0)
# Used only when the mesh carries no vertex colours, so neighbouring automatic parts stay distinguishable.
PALETTE = [(0.85, 0.42, 0.36, 1.0), (0.93, 0.69, 0.33, 1.0), (0.45, 0.72, 0.45, 1.0), (0.35, 0.62, 0.86, 1.0),
           (0.62, 0.48, 0.82, 1.0), (0.82, 0.5, 0.68, 1.0)]
# Rotations that bring a given up axis to +y. Both are cyclic axis permutations (proper rotations, no mirroring).
_TO_Y_UP = {"y": lambda p: p, "z": lambda p: (p[1], p[2], p[0]), "x": lambda p: (p[2], p[0], p[1])}


def orient(mesh: Mesh, up: str) -> Mesh:
    """Rotate so `up` ("x", "y" or "z") becomes +y. For z-up input with the camera on +x (TripoSR's frame),
    (x, y, z) -> (y, z, x) also turns the photographed side to face +z, a glTF viewer's default camera."""
    turn = _TO_Y_UP[up]
    return Mesh([turn(p) for p in mesh.positions], mesh.indices,
                [turn(n) for n in mesh.normals], mesh.colors)


def fit(mesh: Mesh, size: float = 1.0) -> Mesh:
    """Centre the bounding box on the origin and scale its largest side to `size` (metres in USDZ)."""
    lo = [min(p[k] for p in mesh.positions) for k in range(3)]
    hi = [max(p[k] for p in mesh.positions) for k in range(3)]
    centre = [(lo[k] + hi[k]) / 2 for k in range(3)]
    extent = max(hi[k] - lo[k] for k in range(3))
    s = size / extent if extent > 1e-12 else 1.0
    return Mesh([tuple((p[k] - centre[k]) * s for k in range(3)) for p in mesh.positions], mesh.indices,
                mesh.normals, mesh.colors)


def components(mesh: Mesh) -> list[list[int]]:
    """Triangle indices (face numbers) grouped by shared vertices, largest group first."""
    parent = list(range(len(mesh.positions)))

    def root(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    idx = mesh.indices
    for i in range(0, len(idx), 3):
        a = root(idx[i])
        for v in (idx[i + 1], idx[i + 2]):
            r = root(v)
            if r != a:
                parent[r] = a
    groups: dict[int, list[int]] = {}
    for f in range(len(idx) // 3):
        groups.setdefault(root(idx[3 * f]), []).append(f)
    return sorted(groups.values(), key=len, reverse=True)


def submesh(mesh: Mesh, faces: list[int]) -> tuple[Mesh, tuple[float, float, float]]:
    """The given faces as their own mesh, positioned relative to its bounding-box centre (returned too)."""
    remap: dict[int, int] = {}
    indices = []
    for f in faces:
        for v in mesh.indices[3 * f:3 * f + 3]:
            if v not in remap:
                remap[v] = len(remap)
            indices.append(remap[v])
    old = list(remap)
    pts = [mesh.positions[v] for v in old]
    centre = tuple((min(p[k] for p in pts) + max(p[k] for p in pts)) / 2 for k in range(3))
    local = [(p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]) for p in pts]
    return Mesh(local, indices, [mesh.normals[v] for v in old] if mesh.normals else [],
                [mesh.colors[v] for v in old] if mesh.colors else []), centre


def _colour(mesh: Mesh, i: int):
    return WHITE if mesh.colors else PALETTE[i % len(PALETTE)]


def component_parts(mesh: Mesh) -> list[Part] | None:
    groups = components(mesh)
    total = len(mesh.indices) // 3
    big = [g for g in groups if len(g) >= MIN_COMPONENT_SHARE * total]
    if len(big) < 2:
        return None
    keep = big[:MAX_COMPONENTS - 1] if len(big) > MAX_COMPONENTS else big
    rest = [f for g in groups[len(keep):] for f in g]  # groups are sorted, so `keep` is their prefix
    parts = []
    for i, faces in enumerate(keep, start=1):
        sub, centre = submesh(mesh, faces)
        parts.append(Part(f"piece-{i}", f"Piece {i} (automatic component)", sub, _colour(mesh, i), centre))
    if rest:
        sub, centre = submesh(mesh, rest)
        parts.append(Part("small-pieces", "Small pieces (automatic component)", sub, _colour(mesh, 0), centre))
    return parts


def slice_parts(mesh: Mesh, layers: int = DEFAULT_LAYERS) -> list[Part]:
    p, idx = mesh.positions, mesh.indices
    ys = [p[v][1] for v in idx]
    lo, hi = min(ys), max(ys)
    height = (hi - lo) or 1.0
    bins: list[list[int]] = [[] for _ in range(layers)]
    for f in range(len(idx) // 3):
        cy = (ys[3 * f] + ys[3 * f + 1] + ys[3 * f + 2]) / 3
        bins[min(layers - 1, int((cy - lo) / height * layers))].append(f)
    bins = [b for b in bins if b]
    mid = (len(bins) - 1) / 2
    parts = []
    for i, faces in enumerate(bins):
        sub, centre = submesh(mesh, faces)
        offset = i - mid
        parts.append(Part(f"layer-{i + 1}", f"Layer {i + 1} (automatic slice)", sub, _colour(mesh, i), centre,
                          explode_dir=(0.0, 1.0 if offset >= 0 else -1.0, 0.0),
                          explode_distance=abs(offset) / mid if mid else 0.0))
    return parts


def split_mesh(mesh: Mesh, layers: int = DEFAULT_LAYERS) -> tuple[str, list[Part]]:
    if not mesh.normals:
        mesh = mesh.with_normals()  # on the whole mesh, so shading stays smooth across the cuts
    parts = component_parts(mesh)
    return ("components", parts) if parts else ("slices", slice_parts(mesh, layers))


def auto_split(path: Path, layers: int = DEFAULT_LAYERS) -> tuple[str, list[dict]] | None:
    """Rewrite a single-mesh GLB as automatic parts. Returns (parts_kind, parts) or None to leave the file as is."""
    data = path.read_bytes()
    mesh = read_mesh(data)
    if mesh is None or not mesh.indices:
        return None
    kind, parts = split_mesh(mesh, layers)
    doc, _ = read_glb(data)
    name = (doc.get("asset", {}).get("extras") or {}).get("name") or "model"
    extras = {k: v for k, v in (doc.get("asset", {}).get("extras") or {}).items() if k != "name"}
    path.write_bytes(build_glb(parts, name=name, extras={**extras, "parts_kind": kind}))
    return kind, [{"name": p.node_name, "label": p.label} for p in parts]
