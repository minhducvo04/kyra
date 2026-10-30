"""Draw the architecture diagram from architecture.json.

Run from anywhere: python docs/img/architecture.py
It writes architecture-{wide,narrow}-{light,dark}.svg next to this file for the README
(<picture> picks one by colour scheme and screen width) and docs/arch.html, a
self-contained page where choosing a group or a box lights it up with its connections.
All five come from the one data file, so their words cannot disagree; this script only
places them. Dots travel along the arrows in the order a request moves (SMIL), and are
hidden when the reader asks for reduced motion.
"""
import json
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
DATA = json.loads((OUT / "architecture.json").read_text())
FONT = "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
CHAR = 8.43  # advance of one 14 px monospace character
CYCLE = 9  # seconds for one pass of every flow
REPO = "https://github.com/minhducvo04/kyra/blob/main/"

BASE = {
    "light": {"bg": "#ffffff", "frame": "#d1d9e0", "card": "#f6f8fa", "ink": "#1f2328", "ink2": "#59636e"},
    "dark": {"bg": "#0d1117", "frame": "#30363d", "card": "#161b22", "ink": "#e6edf3", "ink2": "#9da7b3"},
}


def theme(mode):
    t = dict(BASE[mode])
    t.update({k: v[mode] for k, v in DATA["kinds"].items()})
    t["outcome"] = t["ink"]
    return t


VAR_THEME = {k: f"var(--{k})" for k in theme("light")}


class Svg:
    def __init__(self, w, h, t, uid=""):
        self.w, self.h, self.t, self.uid, self.parts = w, h, t, uid, []

    def text(self, x, y, s, fill="ink2", size=14, weight=400, anchor="start", cls=""):
        c = f' class="{cls}"' if cls else ""
        return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{self.t[fill]}" '
                f'text-anchor="{anchor}"{c}>{escape(s)}</text>')

    def legend(self, rows):
        """rows: list of (y, [kind ids or 'boundary'])"""
        for y, items in rows:
            x = 16
            for k in items:
                name = "Boundary" if k == "boundary" else DATA["kinds"][k]["name"]
                w = round(len(name) * CHAR + 24)
                stroke = self.t["ink2"] if k == "boundary" else self.t[k]
                dash = ' stroke-dasharray="6 5"' if k == "boundary" else ""
                self.parts.append(
                    f'<g class="legend"><rect x="{x}" y="{y}" width="{w}" height="28" rx="14" fill="{self.t["card"]}" '
                    f'stroke="{stroke}" stroke-width="1.5"{dash}/>'
                    + self.text(x + 12, y + 19, name, fill="ink2" if k == "boundary" else k) + "</g>")
                x += w + 10

    def box(self, key, x, y, w, h, label_x=16):
        self.parts.append(
            f'<g class="boundary"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="none" '
            f'stroke="{self.t["ink2"]}" stroke-width="1.2" stroke-dasharray="6 5"/>'
            + self.text(x + label_x, y + 24, DATA["boundaries"][key].upper(), weight=700) + "</g>")

    def card(self, nid, x, y, w, h):
        n = DATA["nodes"][nid]
        kind = n["kind"]
        width = 2.2 if kind == "outcome" else 1.6
        muted = n.get("muted", False)
        dash = ' stroke-dasharray="6 4"' if muted else ""
        out = [f'<g class="card" data-id="{nid}" data-group="{n["group"]}">'
               f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{self.t["card"]}" '
               f'stroke="{self.t["ink2" if muted else kind]}" stroke-width="{width}"{dash}/>']
        lines = n["lines"]
        if not lines:
            out.append(self.text(x + 12, y + h / 2 + 5.5, n["title"], fill=kind, size=16, weight=700))
        elif h < 50 and kind != "outcome":
            out.append(self.text(x + 12, y + h / 2 + 5.5, n["title"], fill=kind, size=16, weight=700))
            sub_x = x + 24 + round(len(n["title"]) * CHAR * 16 / 14)
            out.append(self.text(sub_x, y + h / 2 + 5, lines[0]))
        elif kind == "outcome":
            ink = "ink2" if muted else kind
            out.append(self.text(x + 12, y + 19, n["title"], fill=ink, weight=700))
            out.append(self.text(x + 12, y + 37, lines[0], fill=ink, weight=700))
        else:
            out.append(self.text(x + 12, y + 24, n["title"], fill=kind, size=16, weight=700))
            out += [self.text(x + 12, y + 45 + i * 18, line) for i, line in enumerate(lines)]
        self.parts.append("".join(out) + "</g>")

    def edge(self, eid, d, labels):
        """labels: list of (x, y, anchor, text or None for the data label)."""
        e = DATA["edges"][eid]
        color = e.get("kind", "ink2")
        dash = ' stroke-dasharray="5 4"' if color == "memory" else ""
        c = self.t[color]
        out = [f'<g class="edge" data-from="{e["from"]}" data-to="{e["to"]}">'
               f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{2 if color == "team" else 1.5}"{dash} '
               f'marker-end="url(#a{self.uid}-{color})"/>']
        out += [self.text(x, y, s or e["label"], anchor=anchor) for x, y, anchor, s in labels]
        s0, e0 = e["flow"]
        out.append(
            f'<circle class="dot" r="4" fill="{c}" stroke="{self.t["bg"]}" stroke-width="1.5" opacity="0">'
            f'<animateMotion dur="{CYCLE}s" repeatCount="indefinite" path="{d}" calcMode="linear" '
            f'keyPoints="0;0;1;1" keyTimes="0;{s0};{e0};1"/>'
            f'<animate attributeName="opacity" dur="{CYCLE}s" repeatCount="indefinite" values="0;0;1;1;0;0" '
            f'keyTimes="0;{s0};{s0 + 0.005:.3f};{e0};{e0 + 0.005:.3f};1"/></circle>')
        self.parts.append("".join(out) + "</g>")

    def render(self, standalone=True):
        markers = "".join(
            f'<marker id="a{self.uid}-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
            f'markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
            f'fill="{self.t[k]}"/></marker>' for k in ("ink2", "team", "memory"))
        style = "<style>@media (prefers-reduced-motion: reduce) { .dot { display: none; } }</style>"
        size = f'width="{self.w}" height="{self.h}" ' if standalone else ""
        label = escape(DATA["label"])
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" {size}role="img" '
                f'aria-label="{label}" font-family="{FONT}"><title>{label}</title>'
                f'{style if standalone else ""}<defs>{markers}</defs>'
                f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="12" fill="{self.t["bg"]}" '
                f'stroke="{self.t["frame"]}"/>' + "".join(self.parts) + "</svg>\n")


def wide(t, uid=""):
    s = Svg(960, 666, t, uid)
    s.legend([(16, ["team", "memory", "models", "other", "boundary"])])
    s.box("private", 16, 60, 600, 112)
    s.box("outside", 632, 60, 312, 112)
    s.box("public", 16, 190, 928, 460, label_x=88)
    # Edges first, so cards sit on top of the line ends.
    s.edge("talks", "M48,150 V264 H102", [(56, 256, "start", None)])
    s.edge("asks", "M48,150 V460 H102", [(56, 452, "start", None)])
    s.edge("opens", "M48,150 V632 H630 V610", [(300, 626, "middle", None)])
    s.edge("text", "M284,264 H358", [(321, 256, "middle", None)])
    s.edge("tolocal", "M560,264 H634", [(597, 256, "middle", None)])
    s.edge("tohosted", "M460,228 V180 H788 V152", [(470, 222, "start", None)])
    s.edge("packrouter", "M420,328 V302", [(428, 320, "start", None)])
    s.edge("nodes", "M264,460 H334", [(299, 452, "middle", None)])
    s.edge("packbuild", "M400,376 V410", [(392, 398, "end", None)])
    s.edge("inbox", "M440,412 V378", [(448, 398, "start", None)])
    s.edge("branch", "M496,460 H566", [(531, 452, "middle", None)])
    s.edge("pass", "M728,435 H782", [(755, 428, "middle", None)])
    s.edge("fail", "M728,487 H782", [(755, 480, "middle", None)])
    s.edge("glb", "M404,580 H478", [(441, 572, "middle", None)])
    for nid, box in {
        "owner": (32, 98, 120, 52), "data": (172, 98, 200, 52), "private": (392, 98, 208, 52),
        "hosted": (648, 98, 280, 52), "voice": (104, 228, 180, 72), "router": (360, 228, 200, 72),
        "local": (636, 228, 180, 72), "memory": (104, 328, 824, 48), "plan": (104, 412, 160, 96),
        "build": (336, 412, 160, 96), "land": (568, 412, 160, 96), "landed": (784, 412, 144, 46),
        "refused": (784, 464, 144, 46), "cad": (104, 552, 300, 56), "viewer": (480, 552, 300, 56),
    }.items():
        s.card(nid, *box)
    return s


def narrow(t, uid=""):
    s = Svg(400, 1340, t, uid)
    s.legend([(16, ["team", "memory", "models"]), (52, ["other", "boundary"])])
    s.box("private", 16, 96, 368, 152)
    s.box("outside", 44, 264, 340, 96)
    s.box("public", 16, 376, 368, 948)
    s.edge("talks", "M32,152 H28 V420 H60 V434", [(68, 430, "start", None)])
    s.edge("asks", "M32,152 H28 V696 H60 V710", [(68, 708, "start", None)])
    s.edge("opens", "M32,152 H28 V1232 H60 V1254", [(68, 1248, "start", None)])
    s.edge("text", "M120,488 V520", [(128, 510, "start", None)])
    s.edge("tolocal", "M176,557 H226", [(201, 549, "middle", None)])
    s.edge("tohosted", "M164,522 V512 H376 V325 H370", [(368, 503, "end", None)])
    s.edge("packrouter", "M100,622 V594", [(108, 613, "start", None)])
    s.edge("nodes", "M164,782 V808", [(172, 800, "start", None)])
    s.edge("packbuild", "M300,674 V846 H286", [(306, 704, "start", "context"), (306, 720, "start", "pack")])
    s.edge("inbox", "M284,872 H372 V676", [(366, 760, "end", "inbox"), (366, 776, "end", "lines")])
    s.edge("branch", "M164,898 V924", [(172, 916, "start", None)])
    s.edge("pass", "M164,1014 V1040", [(172, 1032, "start", None)])
    s.edge("fail", "M284,970 H320 V1117 H286", [(326, 1060, "start", None)])
    s.edge("glb", "M200,1212 V1254", [(208, 1238, "start", None)])
    for nid, box in {
        "owner": (32, 132, 336, 40), "data": (32, 182, 160, 52), "private": (200, 182, 168, 52),
        "hosted": (60, 298, 308, 52), "voice": (44, 436, 324, 52), "router": (44, 522, 132, 70),
        "local": (228, 522, 140, 70), "memory": (44, 622, 324, 52), "plan": (44, 712, 240, 70),
        "build": (44, 810, 240, 88), "land": (44, 926, 240, 88), "landed": (44, 1042, 240, 46),
        "refused": (44, 1094, 240, 46), "cad": (44, 1160, 324, 52), "viewer": (44, 1256, 324, 52),
    }.items():
        s.card(nid, *box)
    return s


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kyra architecture</title>
<style>
:root { %(light)s --link: #0969da; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { %(dark)s --link: #4493f8; color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.55 %(font)s; }
main { max-width: 1000px; margin: 0 auto; padding-block: 32px 48px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
h1 { font-size: 24px; line-height: 1.2; margin: 0 0 6px; }
p.lede { margin: 0 0 18px; color: var(--ink2); max-width: 70ch; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }
.chips button { font: inherit; padding: 5px 14px; border-radius: 999px; cursor: pointer;
  border: 1.5px solid var(--frame); background: var(--card); color: var(--ink); }
.chips button[aria-pressed="true"] { border-color: var(--ink); background: var(--ink); color: var(--bg); }
.chips button:focus-visible { outline: 2px solid var(--team); outline-offset: 2px; }
.card:focus { outline: none; }
.card:focus-visible rect { stroke-width: 3.5; }
figure { margin: 0; }
svg { display: block; width: 100%%; height: auto; }
.narrow { display: none; }
@media (max-width: 720px) { .wide { display: none; } .narrow { display: block; } }
.card { cursor: pointer; }
.card, .edge, .boundary { transition: opacity 160ms ease; }
svg.focus .card:not(.on), svg.focus .edge:not(.on) { opacity: 0.15; }
svg.focus .boundary { opacity: 0.5; }
svg.focus .card.on rect { stroke-width: 3; }
.detail { margin-top: 16px; padding: 14px 16px; border: 1.5px solid var(--frame); border-radius: 10px; background: var(--card); min-height: 92px; }
.detail h2 { margin: 0 0 4px; font-size: 16px; }
.detail p { margin: 0; color: var(--ink2); max-width: 80ch; }
.detail a { color: var(--link); }
@media (prefers-reduced-motion: reduce) { .dot { display: none; } .card, .edge, .boundary { transition: none; } }
</style>
</head>
<body>
<main>
<h1>Kyra architecture</h1>
<p class="lede">Dots follow a request through the system. Choose a group or a box to light it up with its connections. Choose it again, or press Escape, to see everything.</p>
<div class="chips" role="toolbar" aria-label="Groups">
<button type="button" data-group="" aria-pressed="true">Everything</button>
%(chips)s
</div>
<figure>
<div class="wide">%(wide)s</div>
<div class="narrow">%(narrow)s</div>
</figure>
<section class="detail" aria-live="polite"><h2 id="d-title">Everything</h2><p id="d-text">%(all)s</p></section>
</main>
<script>
const NODES = %(nodes)s, GROUPS = %(groups)s, REPO = %(repo)s;
const ALL = document.getElementById('d-text').textContent;
const svgs = [...document.querySelectorAll('figure svg')];
let current = null;
const full = n => NODES[n].title + (NODES[n].kind === 'outcome' ? ' ' + NODES[n].lines[0] : '');
function light(kind, key) {
  const same = current && current[0] === kind && current[1] === key;
  current = key && !same ? [kind, key] : null;
  document.querySelectorAll('.chips button').forEach(b => b.setAttribute('aria-pressed',
    String(current ? kind === 'group' && b.dataset.group === key : b.dataset.group === '')));
  const ids = new Set(!current ? [] : kind === 'group'
    ? Object.keys(NODES).filter(n => NODES[n].group === key) : [key]);
  for (const svg of svgs) {
    svg.classList.toggle('focus', !!current);
    svg.querySelectorAll('.on').forEach(e => e.classList.remove('on'));
    const lit = new Set(ids);
    svg.querySelectorAll('.edge').forEach(e => {
      if (ids.has(e.dataset.from) || ids.has(e.dataset.to)) { e.classList.add('on'); lit.add(e.dataset.from); lit.add(e.dataset.to); }
    });
    svg.querySelectorAll('.card').forEach(c => c.classList.toggle('on', lit.has(c.dataset.id)));
  }
  const t = document.getElementById('d-title'), p = document.getElementById('d-text');
  if (!current) { t.textContent = 'Everything'; p.textContent = ALL; return; }
  if (kind === 'group') {
    t.textContent = GROUPS[key];
    p.textContent = [...ids].map(full).join('; ') + '. Choose a box for details.';
    return;
  }
  const n = NODES[key];
  t.textContent = full(key);
  p.textContent = n.info + ' ';
  if (n.readme) { const a = document.createElement('a'); a.href = REPO + n.readme; a.textContent = 'Read the part README'; p.append(a); }
}
document.querySelectorAll('.chips button').forEach(b => b.addEventListener('click', () => light('group', b.dataset.group)));
for (const svg of svgs) svg.querySelectorAll('.card').forEach(c => {
  const n = NODES[c.dataset.id];
  c.setAttribute('tabindex', '0'); c.setAttribute('role', 'button'); c.setAttribute('aria-label', n.title);
  c.addEventListener('click', () => light('item', c.dataset.id));
  c.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); light('item', c.dataset.id); } });
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') light(null, null); });
</script>
</body>
</html>
"""


def page():
    def tokens(mode):
        return " ".join(f"--{k}: {v};" for k, v in theme(mode).items())

    chips = "\n".join(f'<button type="button" data-group="{g}" aria-pressed="false">{escape(name)}</button>'
                      for g, name in DATA["groups"].items())
    return PAGE % {
        "light": tokens("light"), "dark": tokens("dark"), "font": FONT, "chips": chips,
        "wide": wide(VAR_THEME, "w").render(standalone=False),
        "narrow": narrow(VAR_THEME, "n").render(standalone=False),
        "all": escape(DATA["label"]), "nodes": json.dumps(DATA["nodes"]),
        "groups": json.dumps(DATA["groups"]), "repo": json.dumps(REPO),
    }


def main():
    for mode in ("light", "dark"):
        (OUT / f"architecture-wide-{mode}.svg").write_text(wide(theme(mode)).render())
        (OUT / f"architecture-narrow-{mode}.svg").write_text(narrow(theme(mode)).render())
    (OUT.parent / "arch.html").write_text(page())


if __name__ == "__main__":
    main()
