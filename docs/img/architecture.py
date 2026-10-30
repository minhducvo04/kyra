"""Draw the README architecture diagram as four SVG files.

Run from anywhere: python docs/img/architecture.py
It writes architecture-{wide,narrow}-{light,dark}.svg next to this file.
The README picks one with <picture> by colour scheme and screen width.
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

THEMES = {
    "light": {
        "bg": "#ffffff", "edge": "#d1d9e0", "card": "#f6f8fa", "line": "#c4ccd5",
        "ink": "#1f2328", "ink2": "#59636e", "eyebrow": "#6e7781",
        "acc": "#2459c7", "acc_soft": "#eaf1fd", "mem": "#a95300", "mem_soft": "#fdf4ea",
    },
    "dark": {
        "bg": "#0d1117", "edge": "#30363d", "card": "#161b22", "line": "#3d444d",
        "ink": "#e6edf3", "ink2": "#9da7b3", "eyebrow": "#8b949e",
        "acc": "#79a6ff", "acc_soft": "#132541", "mem": "#f0a35e", "mem_soft": "#2a1c0f",
    },
}


class Svg:
    def __init__(self, w, h, t, label):
        self.w, self.h, self.t, self.parts = w, h, t, []
        self.label = label

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=13, weight=400, fill="ink", anchor="start", rotate=None, spacing=None):
        extra = f' transform="rotate({rotate} {x} {y})"' if rotate is not None else ""
        if spacing:
            extra += f' letter-spacing="{spacing}"'
        self.add(
            f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{self.t[fill]}" '
            f'text-anchor="{anchor}"{extra}>{escape(s)}</text>'
        )

    def eyebrow(self, x, y, s):
        self.text(x, y, s.upper(), size=12, weight=600, fill="eyebrow", spacing="0.08em")

    def card(self, x, y, w, h, title, lines=(), kind="plain", title_size=15, sub_x=None, pad_top=0):
        fill, stroke = {
            "plain": ("card", "line"),
            "acc": ("acc_soft", "acc"),
            "mem": ("mem_soft", "mem"),
        }[kind]
        self.add(f'<g class="card"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
                 f'fill="{self.t[fill]}" stroke="{self.t[stroke]}" stroke-width="1.2"/>')
        if sub_x is None:
            self.text(x + 12, y + pad_top + 24, title, size=title_size, weight=600)
            for i, line in enumerate(lines):
                self.text(x + 12, y + pad_top + 44 + i * 17, line, size=13, fill="ink2")
        else:
            n = len(lines)
            top = y + h / 2 - (n - 1) * 8.5 + 4.5
            self.text(x + 12, y + h / 2 + 5.5, title, size=title_size, weight=600)
            for i, line in enumerate(lines):
                self.text(x + sub_x, top + i * 17, line, size=13, fill="ink2")
        self.add("</g>")

    def path(self, d, color="ink2", dash=False, arrow=True, width=1.5):
        c = self.t[color]
        marker = f' marker-end="url(#a-{color})"' if arrow else ""
        dash_attr = ' stroke-dasharray="5 4"' if dash else ""
        self.add(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{width}"{dash_attr}{marker}/>')

    def render(self):
        markers = "".join(
            f'<marker id="a-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
            f'markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{self.t[k]}"/></marker>'
            for k in ("ink2", "acc", "mem")
        )
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
            f'height="{self.h}" role="img" aria-label="{escape(self.label)}" font-family="{FONT}">'
            f"<title>{escape(self.label)}</title><defs>{markers}</defs>"
            f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="12" '
            f'fill="{self.t["bg"]}" stroke="{self.t["edge"]}"/>'
        )
        return head + "".join(self.parts) + "</svg>\n"


LABEL = (
    "How Kyra fits together. The owner talks through voice or text to a turn router that picks a local "
    "model or a hosted one behind a labelled gate with a daily spend cap. A feature request is "
    "planned into a DAG by the Board and planners, built by agent lanes, tested, and landed on master by "
    "a fast-forward gate; a keeper holds the queue when master is red. Memory gives the router and the "
    "lanes context packs and takes inbox lines back. CAD builds parts as GLB for the 3D viewer."
)

SPINE = [
    ("Plan", ["Board, planners", "split it into a", "feature DAG"], ["Board and planners split", "the request into a DAG"]),
    ("Build", ["Codex, Claude,", "Cursor, Grok and", "local Qwen lanes"], ["Codex, Claude, Cursor, Grok", "and local Qwen lanes"]),
    ("Test", ["impacted tests,", "privacy guards", "and ruff"], ["impacted tests, privacy", "guards and ruff"]),
    ("Land", ["reviewed sha only,", "fast-forward", "push"], ["reviewed sha only,", "fast-forward push"]),
    ("master", ["keeper runs the", "full suite on", "every new sha"], ["keeper runs the full", "suite on every new sha"]),
]
ROUTER = ["local or hosted,", "tool calls hosted,", "each choice logged"]
LOCAL = "offline, never downloads"
HOSTED = "labelled gate, daily cap"
MEMORY = ["tenant-isolated stores, versioned facts,", "erasure with an audit log"]
CAD = ["JSON part spec, sketch solver, build123d"]
VIEWER = ["three.js; one mate solver in Python and JS"]
LOOP = "red master: the keeper holds the queue, sends a fix or a revert"


def wide(t):
    s = Svg(960, 600, t, LABEL)
    x0 = 164
    rb = 300  # top of the code path row
    mid = rb + 48
    rd = rb + 196  # top of the make row
    # Owner and the trunk to the three rows.
    s.card(24, mid - 32, 100, 64, "Owner", ["text, voice"], title_size=16)
    s.path(f"M124,{mid} H140", arrow=False)
    s.path(f"M140,100 V{rd + 78}", arrow=False)
    for y in (100, mid):
        s.path(f"M140,{y} H{x0 - 2}")
    s.path(f"M140,{rd + 78} H756 V{rd + 62}")

    # Row A: answer a turn.
    s.eyebrow(x0, 46, "Owner talks")
    s.card(x0, 56, 176, 88, "Voice or text", ["push to talk or VAD,", "local speech to text"])
    s.path(f"M{x0 + 176},100 H374")
    s.card(376, 56, 190, 88, "Turn router", ROUTER)
    s.path("M566,100 H584 V76 H604")
    s.path("M584,100 V124 H604")
    s.card(606, 56, 330, 40, "Local models", [LOCAL], sub_x=122, title_size=14)
    s.card(606, 104, 330, 40, "Hosted models", [HOSTED], sub_x=122, title_size=14)

    # Memory band between the rows.
    s.card(x0, 180, 936 - x0, 48, "Memory", [" ".join(MEMORY)], kind="mem", sub_x=86)
    s.path("M471,180 V148", color="mem", dash=True)
    s.text(479, 168, "context pack", size=13, fill="ink2")

    # Row B: the code path to master.
    s.eyebrow(x0, rb - 10, "Owner requests a feature")
    step = 158.5
    for i, (title, lines, _) in enumerate(SPINE):
        x = x0 + i * step
        s.card(x, rb, 138, 96, title, lines, kind="acc" if title == "master" else "plain", title_size=17)
        if i:
            s.path(f"M{x - step + 138},{mid} H{x - 2}", color="acc", width=2)
    build_x = x0 + step + 69
    s.path(f"M{build_x + 4},228 V{rb - 2}", color="mem", dash=True)
    s.text(build_x - 2, 251, "context pack", size=13, fill="ink2", anchor="end")
    s.path(f"M{build_x + 36},{rb} V230", color="mem", dash=True)
    s.text(build_x + 42, 251, "inbox lines", size=13, fill="ink2")
    master_x = x0 + 4 * step + 69
    s.path(f"M{master_x},{rb + 96} V{rb + 124} H{build_x} V{rb + 98}", dash=True)
    s.text((master_x + build_x) / 2, rb + 143, LOOP, size=13, fill="ink2", anchor="middle")

    # Row D: CAD builds a model, the owner opens it in the viewer.
    s.eyebrow(x0, rd - 10, "Owner opens a model")
    s.card(x0, rd, 340, 60, "CAD kernel", CAD)
    s.card(576, rd, 360, 60, "3D viewer", VIEWER)
    s.path(f"M504,{rd + 30} H574")
    s.text(540, rd + 22, "GLB", size=13, fill="ink2", anchor="middle")
    return s.render()


def narrow(t):
    s = Svg(400, 1104, t, LABEL)
    s.card(16, 16, 368, 44, "Owner", ["text, voice"], sub_x=80, title_size=16)
    s.path("M200,60 V106")
    # Answer a turn.
    s.eyebrow(16, 98, "Owner talks")
    s.card(16, 108, 368, 60, "Voice or text", ["push to talk or VAD, local speech to text"])
    s.path("M90,168 V192")
    s.card(16, 194, 156, 112, "Turn router", ROUTER, pad_top=12)
    s.path("M172,250 H182 V220 H192")
    s.path("M182,250 V280 H192")
    s.card(194, 194, 190, 52, "Local models", [LOCAL], title_size=14)
    s.card(194, 254, 190, 52, "Hosted models", [HOSTED], title_size=14)
    # Memory.
    s.card(16, 336, 368, 74, "Memory", MEMORY, kind="mem")
    s.path("M90,336 V310", color="mem", dash=True)
    s.text(98, 327, "context pack", size=13, fill="ink2")
    # Code path.
    s.eyebrow(16, 456, "Owner requests a feature")
    by = 556
    s.path(f"M316,410 V{by + 22} H294", color="mem", dash=True)
    s.text(310, 432, "context pack", size=13, fill="ink2", anchor="end")
    s.path(f"M292,{by + 46} H340 V412", color="mem", dash=True)
    s.text(346, 430, "inbox", size=13, fill="ink2")
    s.text(346, 446, "lines", size=13, fill="ink2")
    for i, (title, _, lines) in enumerate(SPINE):
        y = 466 + i * 90
        s.card(16, y, 276, 70, title, lines, kind="acc" if title == "master" else "plain", title_size=16)
        if i:
            s.path(f"M154,{y - 20} V{y - 2}", color="acc", width=2)
    s.path(f"M292,861 H366 V{by + 62} H294", dash=True)
    s.text(300, 832, "red: fix", size=13, fill="ink2")
    s.text(300, 848, "or revert", size=13, fill="ink2")
    # CAD builds a model, the owner opens it in the viewer.
    s.eyebrow(16, 936, "Owner opens a model")
    s.card(16, 946, 368, 58, "CAD kernel", CAD)
    s.path("M100,1004 V1028")
    s.text(108, 1021, "GLB", size=13, fill="ink2")
    s.card(16, 1030, 368, 58, "3D viewer", VIEWER)
    return s.render()


def main():
    for name, t in THEMES.items():
        (OUT / f"architecture-wide-{name}.svg").write_text(wide(t))
        (OUT / f"architecture-narrow-{name}.svg").write_text(narrow(t))


if __name__ == "__main__":
    main()
