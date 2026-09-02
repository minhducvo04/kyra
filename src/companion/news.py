"""Daily tech news, via legitimate RSS/Atom feeds - not scraping.

Scraping NYTimes (or anyone else) directly is fragile - it breaks on any
layout change, is against most sites' terms, and most of the content is
paywalled anyway. RSS is the intended, ToS-friendly path: outlets publish
these feeds specifically to be consumed programmatically. Even NYTimes
itself publishes a free official Technology RSS feed (headline + summary,
no paywall bypass needed) - that's the "correct" way to get NYT headlines,
not crawling the site. See docs/agentic-roadmap.md, job #5.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass

from companion.tools import Tool

ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")

# NYT Technology is official/free RSS (nytimes.com/rss); the rest are each
# outlet's own official feed. Verge publishes Atom, the others RSS 2.0 -
# _parse_feed() below handles both.
FEEDS = {
    "NYT Technology": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Hacker News (front page)": "https://hnrss.org/frontpage",
}


@dataclass
class Headline:
    source: str
    title: str
    summary: str
    link: str


def _clean(text: str, max_len: int = 220) -> str:
    text = _TAG_RE.sub("", text or "").strip()
    return text[:max_len].strip()


def _parse_feed(source: str, root: ET.Element, limit: int) -> list[Headline]:
    items = root.findall(".//item")  # RSS 2.0
    if items:
        return [
            Headline(
                source=source,
                title=_clean(it.findtext("title") or ""),
                summary=_clean(it.findtext("description") or ""),
                link=(it.findtext("link") or "").strip(),
            )
            for it in items[:limit]
        ]

    entries = root.findall(f".//{ATOM_NS}entry")  # Atom (e.g. The Verge)
    out = []
    for e in entries[:limit]:
        link_el = e.find(f"{ATOM_NS}link")
        out.append(
            Headline(
                source=source,
                title=_clean(e.findtext(f"{ATOM_NS}title") or ""),
                summary=_clean(e.findtext(f"{ATOM_NS}summary") or e.findtext(f"{ATOM_NS}content") or ""),
                link=(link_el.get("href") if link_el is not None else "") or "",
            )
        )
    return out


def _fetch_feed(source: str, url: str, limit: int) -> list[Headline]:
    req = urllib.request.Request(url, headers={"User-Agent": "Kyra/1.0 (personal assistant; +local use only)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    return _parse_feed(source, root, limit)


class TechNewsTool(Tool):
    name = "tech_news"
    description = (
        "Fetch today's top tech headlines from a fixed set of official RSS feeds "
        "(NYT Technology, TechCrunch, Ars Technica, The Verge, Hacker News front page). "
        "Use this when Duc asks for tech news or what's happening in tech today."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "per_source": {"type": "integer", "description": "How many headlines per source (default 3)"},
        },
        "required": [],
    }

    def run(self, per_source: int = 3) -> dict:
        headlines: list[Headline] = []
        errors: list[str] = []
        for source, url in FEEDS.items():
            try:
                headlines.extend(_fetch_feed(source, url, per_source))
            except Exception as e:
                # One dead/renamed feed shouldn't sink the whole briefing.
                errors.append(f"{source}: {type(e).__name__}: {e}")
        return {"headlines": [asdict(h) for h in headlines], "errors": errors}
