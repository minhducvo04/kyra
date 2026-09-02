"""Shared RSS/Atom fetching for anything that pulls headlines from
official feeds - tech news (news.py) and science facts (science.py) are
the two current users. Scraping a site directly is fragile (breaks on any
layout change, against most sites' terms, often paywalled anyway) - RSS
is the intended, ToS-friendly path outlets publish specifically to be
consumed programmatically. See docs/agentic-roadmap.md, job #5.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class FeedItem:
    source: str
    title: str
    summary: str
    link: str


def _clean(text: str, max_len: int = 220) -> str:
    text = _TAG_RE.sub("", text or "").strip()
    return text[:max_len].strip()


def _parse_feed(source: str, root: ET.Element, limit: int) -> list[FeedItem]:
    items = root.findall(".//item")  # RSS 2.0
    if items:
        return [
            FeedItem(
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
            FeedItem(
                source=source,
                title=_clean(e.findtext(f"{ATOM_NS}title") or ""),
                summary=_clean(e.findtext(f"{ATOM_NS}summary") or e.findtext(f"{ATOM_NS}content") or ""),
                link=(link_el.get("href") if link_el is not None else "") or "",
            )
        )
    return out


def fetch_feed(source: str, url: str, limit: int) -> list[FeedItem]:
    req = urllib.request.Request(url, headers={"User-Agent": "Kyra/1.0 (personal assistant; +local use only)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    return _parse_feed(source, root, limit)


def fetch_feeds(feeds: dict[str, str], per_source: int) -> tuple[list[FeedItem], list[str]]:
    """Fetch a {source_name: url} dict of feeds, RSS 2.0 or Atom, mixed.
    Returns (items, errors) - one dead/renamed feed shouldn't sink the
    whole briefing, so failures are collected, not raised.
    """
    items: list[FeedItem] = []
    errors: list[str] = []
    for source, url in feeds.items():
        try:
            items.extend(fetch_feed(source, url, per_source))
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}: {e}")
    return items, errors
