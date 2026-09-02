"""Daily tech news, via legitimate RSS/Atom feeds - not scraping. See
feeds.py for why RSS, not scraping, and the fetch/parse machinery shared
with science.py.
"""
from dataclasses import asdict

from companion.feeds import fetch_feeds
from companion.tools import Tool

# NYT Technology is official/free RSS (nytimes.com/rss); the rest are each
# outlet's own official feed. Verge publishes Atom, the others RSS 2.0 -
# feeds.fetch_feed() handles both.
FEEDS = {
    "NYT Technology": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Hacker News (front page)": "https://hnrss.org/frontpage",
}


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
        items, errors = fetch_feeds(FEEDS, per_source)
        return {"headlines": [asdict(h) for h in items], "errors": errors}
