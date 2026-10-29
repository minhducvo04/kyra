"""Placement lists for pattern_path and pattern_fill. Points only; the kernel still cuts.

pattern_path spaces `count` instances equally by arc length on a polyline or an arc, start first.
pattern_fill lays a grid on a rectangular region from its minimum corner at spacing [dx, dy].
Instances that overlap or leave the solid are refused by the existing pattern cut.
"""
import math

TOL = 1e-9
MAX_INSTANCES = 50


def problem(feature, seen, source) -> str | None:
    """Reason the pattern cannot be placed, or None. `seen` is unused; the geometry is inline."""
    del seen
    try:
        points = placements(feature)
    except (KeyError, TypeError, ValueError) as exc:
        return str(exc)
    if len(points) < 2:
        return "a pattern needs at least 2 instances"
    diameter = float(source["diameter"]) if source.get("kind") == "hole" else 0.0
    if _overlaps(points, diameter):
        return "instances overlap; a pattern refuses instead of clipping"
    return None


def placements(feature) -> list[list[float]]:
    """[x, y] of every instance, the original (path start or region min corner) first."""
    kind = feature.get("kind")
    if kind == "pattern_path":
        return _path(feature["path"], int(feature["count"]))
    if kind == "pattern_fill":
        return _fill(feature["region"], feature["spacing"])
    raise ValueError(f"unknown pattern kind {kind}")


def offsets(feature, sketches, seed_xy, radius=0.0) -> list[tuple[float, float]]:
    """World XY translations from the seed tool to each other instance."""
    del sketches, radius
    points = placements(feature)
    sx, sy = seed_xy
    return [(x - sx, y - sy) for x, y in points if math.hypot(x - sx, y - sy) > 1e-6]


def _path(path, count) -> list[list[float]]:
    if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= MAX_INSTANCES:
        raise ValueError(f"count must be a whole number from 2 to {MAX_INSTANCES}")
    kind = path.get("kind") if isinstance(path, dict) else None
    if kind == "polyline":
        return _along(_segments(path["points"]), count)
    if kind == "arc":
        return _arc(path, count)
    raise ValueError(f"unknown path kind {kind}")


def _segments(points):
    pts = [tuple(p) for p in points]
    segs = []
    for a, b in zip(pts, pts[1:], strict=False):
        length = math.dist(a, b)
        if length <= TOL:
            raise ValueError("polyline segments must have length")
        segs.append((a, b, length))
    if not segs:
        raise ValueError("polyline needs at least two points")
    return segs


def _along(segs, count) -> list[list[float]]:
    total = sum(length for _, _, length in segs)
    out = []
    for i in range(count):
        left = total * i / (count - 1)
        for a, b, length in segs:
            if left <= length + TOL:
                t = 0.0 if length <= TOL else min(1.0, left / length)
                out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
                break
            left -= length
        else:
            end = segs[-1][1]
            out.append([end[0], end[1]])
    return out


def _arc(path, count) -> list[list[float]]:
    """Signed sweep: from start_deg to end_deg as written, so end < start runs clockwise."""
    cx, cy = path["center"]
    radius = float(path["radius"])
    start = math.radians(float(path["start_deg"]))
    end = math.radians(float(path["end_deg"]))
    if radius <= TOL or abs(end - start) <= TOL:
        raise ValueError("arc needs a positive radius and a non-zero sweep")
    points = []
    for i in range(count):
        ang = start + (end - start) * i / (count - 1)
        points.append([cx + radius * math.cos(ang), cy + radius * math.sin(ang)])
    return points


def _fill(region, spacing) -> list[list[float]]:
    if not isinstance(spacing, (list, tuple)) or len(spacing) != 2:
        raise ValueError("spacing must be [dx, dy]")
    dx, dy = float(spacing[0]), float(spacing[1])
    if dx <= TOL or dy <= TOL:
        raise ValueError("spacing must be a positive [dx, dy]")
    cx, cy = region["center"]
    x0 = float(cx) - float(region["width"]) / 2
    y0 = float(cy) - float(region["height"]) / 2
    x1 = float(cx) + float(region["width"]) / 2
    y1 = float(cy) + float(region["height"]) / 2
    cols = int((x1 - x0) / dx + TOL) + 1
    rows = int((y1 - y0) / dy + TOL) + 1
    if cols * rows > MAX_INSTANCES:
        raise ValueError(f"fill would place {cols * rows} instances; the limit is {MAX_INSTANCES}")
    points = []
    j = 0
    while y0 + j * dy <= y1 + TOL:
        y = y0 + j * dy
        i = 0
        while x0 + i * dx <= x1 + TOL:
            points.append([x0 + i * dx, y])
            i += 1
        j += 1
    return points


def _overlaps(points, diameter) -> bool:
    if diameter <= TOL:
        return False
    for i, a in enumerate(points):
        for b in points[i + 1:]:
            if math.dist(a, b) < diameter - 1e-6:
                return True
    return False
