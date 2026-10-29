"""Query-word splitting shared by the pack builder and the host's lesson search."""
import re

STOPWORDS = frozenset(
    "a an the and or of to in on for with is are was were be do does did how what when which why who should must "
    "that this from it its as at by not no".split()
)


def query_words(query: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[\w./-]+", query)]
    return [w for w in words if w not in STOPWORDS] or words
