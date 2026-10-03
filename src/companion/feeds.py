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
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

MAX_FEED_BYTES = 2_000_000


class FeedTooLarge(ValueError):
    """A feed body exceeds the fixed parse budget."""


ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class FeedItem:
    source: str
    title: str
    summary: str
    link: str
    published_at: str | None = None
    date_source: str | None = None


def _clean(text: str, max_len: int = 220) -> str:
    text = _TAG_RE.sub("", text or "").strip()
    return text[:max_len].strip()


def _date(text: str | None, *, atom: bool = False) -> str | None:
    if not text:
        return None
    try:
        if atom:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", text.strip()):
                return None
            parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        else:
            parsed = parsedate_to_datetime(text.strip())
            # Email dates with -0000 denote UTC without a timezone object.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _atom_link(entry: ET.Element) -> str:
    for link in entry.findall(f"{ATOM_NS}link"):
        if link.get("rel", "alternate") not in ("alternate", ""):
            continue
        if link.get("type", "text/html") != "text/html":
            continue
        href = (link.get("href") or "").strip()
        if href:
            return href
    return ""


def _parse_feed(source: str, root: ET.Element, limit: int) -> list[FeedItem]:
    items = root.findall(".//item")  # RSS 2.0
    out = []
    for item in items[:max(0, limit)]:
        published = _date(item.findtext("pubDate"))
        out.append(FeedItem(
            source=source, title=_clean(item.findtext("title") or ""),
            summary=_clean(item.findtext("description") or ""),
            link=(item.findtext("link") or "").strip(),
            published_at=published, date_source="published" if published else None,
        ))
    if items:
        return out
    for entry in root.findall(f".//{ATOM_NS}entry")[:max(0, limit)]:
        published = _date(entry.findtext(f"{ATOM_NS}published"), atom=True)
        date_source = "published" if published else None
        if published is None:
            published = _date(entry.findtext(f"{ATOM_NS}updated"), atom=True)
            date_source = "updated" if published else None
        out.append(FeedItem(
            source=source, title=_clean(entry.findtext(f"{ATOM_NS}title") or ""),
            summary=_clean(entry.findtext(f"{ATOM_NS}summary") or entry.findtext(f"{ATOM_NS}content") or ""),
            link=_atom_link(entry), published_at=published, date_source=date_source,
        ))
    return out


def parse_feed_bytes(source: str, data: bytes, limit: int) -> list[FeedItem]:
    if len(data) > MAX_FEED_BYTES:
        raise FeedTooLarge(f"Feed exceeded {MAX_FEED_BYTES} bytes")
    return _parse_feed(source, ET.fromstring(data), limit)


def fetch_feed(source: str, url: str, limit: int) -> list[FeedItem]:
    req = urllib.request.Request(url, headers={"User-Agent": "Kyra/1.0 (personal assistant; +local use only)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = resp.read(MAX_FEED_BYTES + 1)
    return parse_feed_bytes(source, data, limit)


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
