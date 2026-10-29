"""Bar charts from a load-results JSON, as standalone SVGs. Standard library only.

    python -m kyra_memory.bench.charts --results load-sqlite.json --out img/

Two charts, one idea each: recall p95 by tenant count, and writes per second by
tenant count. Every plotted value is a median field copied from the JSON, never
recomputed here, and the value is printed on its bar. Colours switch with
prefers-color-scheme inside the SVG so the same file reads on a light or a dark
page. Output is deterministic: the same JSON always gives the same bytes.
"""
import argparse
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

WIDTH, HEIGHT = 720, 440
LEFT, RIGHT, TOP, BOTTOM = 76, 24, 104, 88

PALETTES = {
    "light": {"bg": "#ffffff", "fg": "#1f2328", "muted": "#57606a", "grid": "#d0d7de",
              "axis": "#57606a", "bar_a": "#0969da", "bar_b": "#8250df"},
    "dark": {"bg": "#0d1117", "fg": "#e6edf3", "muted": "#9198a1", "grid": "#30363d",
             "axis": "#9198a1", "bar_a": "#4493f8", "bar_b": "#d2a8ff"},
}

RECALL_NAME = "recall-p95-by-tenants.svg"
WRITES_NAME = "writes-per-second-by-tenants.svg"


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two #rrggbb colours."""
    def lum(hex_colour: str) -> float:
        raw = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in raw]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = max(lum(a), lum(b)), min(lum(a), lum(b))
    return (hi + 0.05) / (lo + 0.05)


def load_points(path: Path) -> dict:
    """The parsed load-results JSON, with its points checked and sorted by tenant count."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    points = doc["points"]
    if not points:
        raise ValueError("load results hold no points")
    for point in points:
        for key in ("tenants", "search_p95_s_median", "topk_pack_p95_s_median", "writes_per_s_median"):
            if not isinstance(point.get(key), (int, float)):
                raise ValueError(f"point is missing a numeric {key}")
    doc["points"] = sorted(points, key=lambda p: p["tenants"])
    return doc


def format_ms(seconds: float) -> str:
    return f"{seconds * 1000:.3f}"


def format_writes(value: float) -> str:
    return f"{value:,.2f}"


def _nice_max(value: float) -> float:
    """The smallest 1/1.25/1.6/2/2.5... step at or above `value`, so axis ticks stay round."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    scaled = value / power
    for step in (1.0, 1.25, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.4, 8.0, 10.0):
        if scaled <= step:
            return step * power
    return 10.0 * power


def _style() -> str:
    light, dark = PALETTES["light"], PALETTES["dark"]
    def vars_for(palette: dict) -> str:
        return "".join(f"--{name}:{colour};" for name, colour in palette.items())
    return (
        f":root{{{vars_for(light)}}}"
        f"@media (prefers-color-scheme: dark){{:root{{{vars_for(dark)}}}}}"
        "text{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;fill:var(--fg)}"
        ".title{font-size:20px;font-weight:600}"
        ".subtitle{font-size:12.5px;fill:var(--muted)}"
        ".value{font-size:13px;font-weight:600}"
        ".tick{font-size:12px;fill:var(--muted)}"
        ".axis-label{font-size:13px;fill:var(--muted)}"
        ".legend{font-size:12.5px}"
        ".note{font-size:11.5px;fill:var(--muted)}"
    )


def bar_chart(*, title: str, subtitle: str, note: str, y_label: str, x_label: str,
              groups: list[str], series: list[dict], tick_format) -> str:
    """One grouped bar chart as an SVG string. `series` rows hold name, key, values and labels."""
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    base_y = TOP + plot_h
    top = _nice_max(max(v for row in series for v in row["values"]))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="{escape(title, {chr(34): "&quot;"})}">',
        f"<title>{escape(title)}</title>",
        f"<style>{_style()}</style>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="var(--bg)"/>',
        f'<text class="title" x="{LEFT}" y="34">{escape(title)}</text>',
        f'<text class="subtitle" x="{LEFT}" y="56">{escape(subtitle)}</text>',
    ]
    # legend, top right of the header band
    legend_x = LEFT
    if len(series) > 1:
        for row in series:
            parts.append(f'<rect x="{legend_x}" y="66" width="12" height="12" fill="var(--bar_{row["slot"]})"/>')
            parts.append(f'<text class="legend" x="{legend_x + 18}" y="77">{escape(row["name"])}</text>')
            legend_x += 18 + 8 * len(row["name"]) + 26
    # gridlines and y ticks, four steps
    for step in range(5):
        value = top * step / 4
        y = base_y - plot_h * step / 4
        parts.append(f'<line x1="{LEFT}" y1="{y:.2f}" x2="{LEFT + plot_w}" y2="{y:.2f}" stroke="var(--grid)"/>')
        parts.append(f'<text class="tick" x="{LEFT - 8}" y="{y + 4:.2f}" text-anchor="end">'
                     f'{escape(tick_format(value))}</text>')
    parts.append(f'<line x1="{LEFT}" y1="{TOP}" x2="{LEFT}" y2="{base_y}" stroke="var(--axis)"/>')
    parts.append(f'<line x1="{LEFT}" y1="{base_y}" x2="{LEFT + plot_w}" y2="{base_y}" stroke="var(--axis)"/>')
    # bars: one slot per group, series side by side inside it
    slot_w = plot_w / len(groups)
    bar_w = min(56.0, slot_w * 0.72 / len(series))
    for gi, group in enumerate(groups):
        centre = LEFT + slot_w * (gi + 0.5)
        start_x = centre - bar_w * len(series) / 2
        for si, row in enumerate(series):
            value = row["values"][gi]
            height = plot_h * value / top
            x = start_x + si * bar_w
            parts.append(f'<rect x="{x:.2f}" y="{base_y - height:.2f}" width="{bar_w - 4:.2f}" '
                         f'height="{height:.2f}" fill="var(--bar_{row["slot"]})"/>')
            parts.append(f'<text class="value" data-series="{escape(row["key"])}" data-group="{escape(group)}" '
                         f'x="{x + (bar_w - 4) / 2:.2f}" y="{base_y - height - 6:.2f}" text-anchor="middle">'
                         f'{escape(row["labels"][gi])}</text>')
        parts.append(f'<text class="tick" x="{centre:.2f}" y="{base_y + 20}" text-anchor="middle">'
                     f'{escape(group)}</text>')
    mid_y = TOP + plot_h / 2
    parts.append(f'<text class="axis-label" transform="rotate(-90 20 {mid_y:.2f})" x="20" y="{mid_y:.2f}" '
                 f'text-anchor="middle">{escape(y_label)}</text>')
    parts.append(f'<text class="axis-label" x="{LEFT + plot_w / 2:.2f}" y="{base_y + 44}" '
                 f'text-anchor="middle">{escape(x_label)}</text>')
    lines = note.split("\n")
    for i, line in enumerate(lines):
        y = HEIGHT - 10 - 14 * (len(lines) - 1 - i)
        parts.append(f'<text class="note" x="{LEFT}" y="{y}">{escape(line)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _run_note(doc: dict) -> str:
    runs = doc.get("runs_per_point")
    rows = doc["points"][0]["runs"][0]["rows_per_tenant"] if doc["points"][0].get("runs") else None
    bits = [f"Source: load-{doc.get('backend', 'sqlite')}.json"]
    if runs:
        bits.append(f"median of {runs} runs per point")
    if rows:
        bits.append(f"{rows} rows per tenant")
    return ", ".join(bits) + "."


def recall_chart(doc: dict) -> str:
    """Search p95 and pack p95 (pack includes search), in milliseconds, by tenant count."""
    points = doc["points"]
    return bar_chart(
        title="Recall p95 latency by tenant count",
        subtitle="SQLite backend, sequential queries, warm cache. Lower is faster.",
        note=_run_note(doc) + "\nPack is a sort-and-join stand-in; its time includes search.",
        y_label="p95 latency (ms)",
        x_label="Tenants in one process",
        groups=[str(p["tenants"]) for p in points],
        series=[
            {"name": "Search p95", "key": "search_p95_s_median", "slot": "a",
             "values": [p["search_p95_s_median"] * 1000 for p in points],
             "labels": [format_ms(p["search_p95_s_median"]) for p in points]},
            {"name": "Pack p95 (includes search)", "key": "topk_pack_p95_s_median", "slot": "b",
             "values": [p["topk_pack_p95_s_median"] * 1000 for p in points],
             "labels": [format_ms(p["topk_pack_p95_s_median"]) for p in points]},
        ],
        tick_format=lambda v: f"{v:.2f}",
    )


def writes_chart(doc: dict) -> str:
    """Total writes per second by tenant count."""
    points = doc["points"]
    return bar_chart(
        title="Writes per second by tenant count",
        subtitle="SQLite backend, 4 writer threads per tenant. Higher is faster.",
        note=_run_note(doc) + "\nWrite timing includes tenant creation.",
        y_label="Writes per second (total)",
        x_label="Tenants in one process",
        groups=[str(p["tenants"]) for p in points],
        series=[
            {"name": "Writes per second", "key": "writes_per_s_median", "slot": "a",
             "values": [p["writes_per_s_median"] for p in points],
             "labels": [format_writes(p["writes_per_s_median"]) for p in points]},
        ],
        tick_format=lambda v: f"{v:,.0f}",
    )


def render_all(results: Path, out: Path) -> dict:
    """Write both SVGs under `out`; returns {file name: svg text}."""
    doc = load_points(Path(results))
    charts = {RECALL_NAME: recall_chart(doc), WRITES_NAME: writes_chart(doc)}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, svg in charts.items():
        (out / name).write_text(svg, encoding="utf-8")
    return charts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kyra_memory.bench.charts", description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, required=True, help="load-<backend>.json from the load harness")
    parser.add_argument("--out", type=Path, required=True, help="directory the two SVGs are written to")
    args = parser.parse_args(argv)
    for name in render_all(args.results, args.out):
        print(f"wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
