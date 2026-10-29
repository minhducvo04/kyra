"""Minimal glTF 2.0 binary (GLB) writer and reader, standard library only.

Kyra's 3D pipeline needs exactly one shape of file: a scene whose parts are separate nodes named
`part:<slug>`, each with its local origin at the assembled position, so a viewer can explode or hide
parts. That is small enough to write directly with `struct`; no mesh library is installed in the venv
and requirements files are protected, so nothing here needs one.
"""
import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

GLB_MAGIC = 0x46546C67  # b"glTF"
_JSON = 0x4E4F534A
_BIN = 0x004E4942
_FLOAT, _UINT32, _UBYTE = 5126, 5125, 5121
_ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER = 34962, 34963


@dataclass
class Mesh:
    positions: list[tuple[float, float, float]]
    indices: list[int]
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    colors: list[tuple[int, int, int, int]] = field(default_factory=list)  # optional RGBA 0-255 per vertex

    def with_normals(self) -> "Mesh":
        """Area-weighted vertex normals; parametric seams stay slightly faceted, which is fine for a demo."""
        acc = [[0.0, 0.0, 0.0] for _ in self.positions]
        p = self.positions
        for i in range(0, len(self.indices), 3):
            a, b, c = self.indices[i:i + 3]
            ux, uy, uz = (p[b][k] - p[a][k] for k in range(3))
            vx, vy, vz = (p[c][k] - p[a][k] for k in range(3))
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            for v in (a, b, c):
                for k in range(3):
                    acc[v][k] += n[k]
        normals = []
        for n in acc:
            length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
            normals.append((n[0] / length, n[1] / length, n[2] / length) if length > 1e-12 else (0.0, 1.0, 0.0))
        return Mesh(self.positions, self.indices, normals, self.colors)


@dataclass
class Part:
    slug: str
    label: str
    mesh: Mesh
    color: tuple[float, float, float, float]
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    explode_dir: tuple[float, float, float] | None = None
    explode_distance: float | None = None  # relative travel 0..1 along explode_dir; viewers default to 1
    metallic: float = 0.0
    roughness: float = 0.6
    extras: dict | None = None  # extra node extras, e.g. a library part's "does" sentence

    @property
    def node_name(self) -> str:
        return f"part:{self.slug}"


def slugify(text: str, fallback: str = "part") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")
    return slug or fallback


def _unit(v):
    length = math.sqrt(sum(c * c for c in v))
    return tuple(c / length for c in v) if length > 1e-9 else (0.0, 1.0, 0.0)


def build_glb(parts: list[Part], *, name: str, extras: dict | None = None) -> bytes:
    """One root node holding one `part:<slug>` child per part; one mesh and one material per part."""
    if not parts:
        raise ValueError("a model needs at least one part")
    slugs = [p.slug for p in parts]
    if len(set(slugs)) != len(slugs):
        raise ValueError("part slugs must be unique")
    blob = bytearray()
    views, accessors, meshes, materials, nodes = [], [], [], [], [{"name": f"model:{slugify(name, 'model')}",
                                                                   "children": []}]

    def add_view(data: bytes, target: int) -> int:
        offset = len(blob)
        blob.extend(data)
        blob.extend(b"\0" * (-len(blob) % 4))
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data), "target": target})
        return len(views) - 1

    for part in parts:
        mesh = part.mesh if part.mesh.normals else part.mesh.with_normals()
        count = len(mesh.positions)
        if count == 0 or len(mesh.indices) % 3 or max(mesh.indices) >= count or min(mesh.indices) < 0:
            raise ValueError(f"bad mesh for part {part.slug}")
        flat = [c for v in mesh.positions for c in v]
        pos_view = add_view(struct.pack(f"<{len(flat)}f", *flat), _ARRAY_BUFFER)
        nflat = [c for v in mesh.normals for c in v]
        nrm_view = add_view(struct.pack(f"<{len(nflat)}f", *nflat), _ARRAY_BUFFER)
        attributes = {"POSITION": len(accessors), "NORMAL": len(accessors) + 1}
        if mesh.colors:
            if len(mesh.colors) != count:
                raise ValueError(f"bad vertex colours for part {part.slug}")
            col_view = add_view(bytes(c for rgba in mesh.colors for c in rgba), _ARRAY_BUFFER)
        idx_view = add_view(struct.pack(f"<{len(mesh.indices)}I", *mesh.indices), _ELEMENT_ARRAY_BUFFER)
        # glTF requires min/max on POSITION; computed from the packed float32 values so they match exactly.
        packed = struct.unpack(f"<{len(flat)}f", struct.pack(f"<{len(flat)}f", *flat))
        lo = [min(packed[k::3]) for k in range(3)]
        hi = [max(packed[k::3]) for k in range(3)]
        accessors += [
            {"bufferView": pos_view, "componentType": _FLOAT, "count": count, "type": "VEC3", "min": lo, "max": hi},
            {"bufferView": nrm_view, "componentType": _FLOAT, "count": count, "type": "VEC3"},
            {"bufferView": idx_view, "componentType": _UINT32, "count": len(mesh.indices), "type": "SCALAR"},
        ]
        indices_accessor = len(accessors) - 1
        if mesh.colors:
            attributes["COLOR_0"] = len(accessors)
            accessors.append({"bufferView": col_view, "componentType": _UBYTE, "normalized": True, "count": count,
                              "type": "VEC4"})
        materials.append({"name": part.slug, "doubleSided": True,
                          "alphaMode": "BLEND" if part.color[3] < 1 else "OPAQUE",
                          "pbrMetallicRoughness": {"baseColorFactor": list(part.color),
                                                   "metallicFactor": part.metallic,
                                                   "roughnessFactor": part.roughness}})
        meshes.append({"name": part.slug, "primitives": [
            {"attributes": attributes, "indices": indices_accessor, "material": len(materials) - 1}]})
        node_extras = {**(part.extras or {}), "label": part.label}
        if part.explode_dir is not None:
            node_extras["explode_dir"] = [round(c, 6) for c in _unit(part.explode_dir)]
        if part.explode_distance is not None:
            node_extras["explode_distance"] = round(max(0.0, min(1.0, part.explode_distance)), 6)
        nodes.append({"name": part.node_name, "mesh": len(meshes) - 1,
                      "translation": [float(c) for c in part.translation], "extras": node_extras})
        nodes[0]["children"].append(len(nodes) - 1)

    doc = {"asset": {"version": "2.0", "generator": "kyra-model3d", "extras": {"name": name, **(extras or {})}},
           "scene": 0, "scenes": [{"name": name, "nodes": [0]}], "nodes": nodes, "meshes": meshes,
           "materials": materials, "accessors": accessors, "bufferViews": views,
           "buffers": [{"byteLength": len(blob)}]}
    return pack_glb(doc, bytes(blob))


def pack_glb(doc: dict, blob: bytes) -> bytes:
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True).encode()
    text += b" " * (-len(text) % 4)
    blob += b"\0" * (-len(blob) % 4)
    body = struct.pack("<II", len(text), _JSON) + text
    if blob:
        body += struct.pack("<II", len(blob), _BIN) + blob
    return struct.pack("<III", GLB_MAGIC, 2, 12 + len(body)) + body


def read_glb(data: bytes) -> tuple[dict, bytes]:
    """Return (json, bin). Raises ValueError for anything that is not a well-formed GLB 2.0 container."""
    if len(data) < 20:
        raise ValueError("too short for GLB")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC or version != 2 or length != len(data):
        raise ValueError("not a glTF 2.0 binary")
    jlen, jtype = struct.unpack_from("<II", data, 12)
    if jtype != _JSON or 20 + jlen > len(data):
        raise ValueError("first GLB chunk is not JSON")
    doc = json.loads(data[20:20 + jlen])
    blob, offset = b"", 20 + jlen
    if offset + 8 <= len(data):
        blen, btype = struct.unpack_from("<II", data, offset)
        if btype == _BIN:
            blob = data[offset + 8:offset + 8 + blen]
    return doc, blob


def part_nodes(doc: dict) -> list[dict]:
    """Every `part:*` node as {"name", "label"} in file order."""
    out = []
    for node in doc.get("nodes", []):
        name = node.get("name", "")
        if name.startswith("part:"):
            out.append({"name": name, "label": (node.get("extras") or {}).get("label") or name[5:].replace("-", " ")})
    return out


def normalize_parts(path: Path) -> list[dict]:
    """Make a provider's GLB follow the contract: every mesh node is named `part:<slug>`.

    Files that already have part nodes are left untouched. Others get their mesh nodes renamed in place
    (the binary chunk is copied unchanged). Returns the part list.
    """
    data = path.read_bytes()
    doc, blob = read_glb(data)
    if part_nodes(doc):
        return part_nodes(doc)
    seen = set()
    for i, node in enumerate(doc.get("nodes", [])):
        if "mesh" not in node:
            continue
        base = slugify(node.get("name") or "", f"mesh-{i}")
        slug, n = base, 2
        while slug in seen:
            slug, n = f"{base}-{n}", n + 1
        seen.add(slug)
        extras = node.setdefault("extras", {}) if isinstance(node.get("extras", {}), dict) else {}
        extras.setdefault("label", node.get("name") or f"Mesh {i + 1}")
        node["extras"] = extras
        node["name"] = f"part:{slug}"
    if not seen:
        raise ValueError("model has no mesh nodes")
    path.write_bytes(pack_glb(doc, blob))
    return part_nodes(doc)


_COMPONENT = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_NORM_MAX = {5121: 255.0, 5123: 65535.0}
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def accessor(doc: dict, blob: bytes, index: int) -> list:
    """An accessor's values from the GLB binary chunk: scalars as numbers, vectors as tuples."""
    acc = doc["accessors"][index]
    if "bufferView" not in acc or "sparse" in acc:
        raise ValueError("sparse or empty accessors are not supported")
    view = doc["bufferViews"][acc["bufferView"]]
    if view.get("buffer", 0) != 0 or "uri" in doc["buffers"][0]:
        raise ValueError("only the GLB binary chunk is supported")
    fmt, width, count = _COMPONENT[acc["componentType"]], _WIDTH[acc["type"]], acc["count"]
    size = struct.calcsize(fmt) * width
    stride = view.get("byteStride") or size
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    if count and start + stride * (count - 1) + size > min(len(blob), view.get("byteOffset", 0) + view["byteLength"]):
        raise ValueError("accessor runs past its buffer view")
    if stride == size:
        flat = struct.unpack_from(f"<{count * width}{fmt}", blob, start)
    else:
        flat = [v for i in range(count) for v in struct.unpack_from(f"<{width}{fmt}", blob, start + i * stride)]
    if width == 1:
        return list(flat)
    return [tuple(flat[i:i + width]) for i in range(0, len(flat), width)]


def _matmul(a, b):
    return tuple(sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4))


def node_matrix(node: dict) -> tuple:
    """The node's local transform as a row-major 4x4 (glTF stores `matrix` column-major; else T * R * S)."""
    if "matrix" in node:
        m = node["matrix"]
        return tuple(float(m[c * 4 + r]) for r in range(4) for c in range(4))
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    r = (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
         2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
         2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y))
    return tuple(float(v) for v in (r[0] * sx, r[1] * sy, r[2] * sz, tx, r[3] * sx, r[4] * sy, r[5] * sz, ty,
                                    r[6] * sx, r[7] * sy, r[8] * sz, tz, 0.0, 0.0, 0.0, 1.0))


def vertex_colors(values: list, component: int, normalized: bool) -> list[tuple[int, int, int, int]]:
    scale = 255.0 if component == _FLOAT or not normalized else 255.0 / _NORM_MAX.get(component, 255.0)
    out = []
    for v in values:
        rgba = tuple(min(255, max(0, round(c * scale))) for c in v)
        out.append(rgba if len(rgba) == 4 else (*rgba, 255))
    return out


def read_mesh(data: bytes) -> Mesh | None:
    """The one triangle mesh of a single-mesh GLB, in world space, with vertex colours when present.

    Returns None when the file holds anything other than exactly one untextured triangle primitive (it already has
    parts, carries a texture, or has a layout this reader does not flatten). Normals are kept only when the node transform is the identity.
    """
    doc, blob = read_glb(data)
    nodes, found = doc.get("nodes", []), []
    scene = (doc.get("scenes") or [{"nodes": list(range(len(nodes)))}])[doc.get("scene", 0)]
    stack = [(i, _IDENTITY) for i in scene.get("nodes", [])]
    while stack:
        i, parent = stack.pop()
        world = _matmul(parent, node_matrix(nodes[i]))
        if "mesh" in nodes[i]:
            found += [(prim, world) for prim in doc["meshes"][nodes[i]["mesh"]]["primitives"]]
        stack += [(c, world) for c in nodes[i].get("children", [])]
    if len(found) != 1 or found[0][0].get("mode", 4) != 4:
        return None
    prim, m = found[0]
    attrs = prim["attributes"]
    if any(name.startswith("TEXCOORD") for name in attrs):
        return None  # textured: rewriting it here would drop the texture
    positions = accessor(doc, blob, attrs["POSITION"])
    indices = accessor(doc, blob, prim["indices"]) if "indices" in prim else list(range(len(positions)))
    if m != _IDENTITY:
        positions = [(m[0] * x + m[1] * y + m[2] * z + m[3], m[4] * x + m[5] * y + m[6] * z + m[7],
                      m[8] * x + m[9] * y + m[10] * z + m[11]) for x, y, z in positions]
    normals = accessor(doc, blob, attrs["NORMAL"]) if "NORMAL" in attrs and m == _IDENTITY else []
    colors = []
    if "COLOR_0" in attrs:
        acc = doc["accessors"][attrs["COLOR_0"]]
        colors = vertex_colors(accessor(doc, blob, attrs["COLOR_0"]), acc["componentType"], acc.get("normalized", False))
    if len(indices) % 3 or (indices and max(indices) >= len(positions)):
        raise ValueError("bad triangle indices")
    return Mesh(positions, list(indices), normals, colors)
