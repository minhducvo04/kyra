"""Science facts/headlines, same shape and same reasoning as news.py -
official RSS feeds, not scraping and not model-invented facts. See
docs/agentic-roadmap.md, job #8.
"""
from dataclasses import asdict

from companion.feeds import fetch_feeds
from companion.tools import Tool

FEEDS = {
    "NYT Science": "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "ScienceDaily": "https://www.sciencedaily.com/rss/all.xml",
    "Phys.org": "https://phys.org/rss-feed/",
    "NASA": "https://www.nasa.gov/news-release/feed/",
}


class ScienceFactsTool(Tool):
    name = "science_facts"
    description = (
        "Fetch recent science headlines/facts from official RSS feeds (NYT Science, "
        "ScienceDaily, Phys.org, NASA). Use when Duc asks for a science fact, something "
        "interesting in science, or what's new in science."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "per_source": {"type": "integer", "description": "How many items per source (default 2)"},
        },
        "required": [],
    }

    def run(self, per_source: int = 2) -> dict:
        items, errors = fetch_feeds(FEEDS, per_source)
        return {"facts": [asdict(h) for h in items], "errors": errors}
