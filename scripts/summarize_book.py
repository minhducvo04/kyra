"""Read a book (PDF or EPUB) and save a structured summary into Kyra's
spaced-repetition learning system.

Usage:
    python3 scripts/summarize_book.py path/to/book.pdf
    python3 scripts/summarize_book.py path/to/book.epub --topic "Custom title"

Sourcing the book file is on you - this only reads a file you already
have. See docs/agentic-roadmap.md, job #7, and CLAUDE.md for why this is
a standalone script rather than a conversational tool: summarizing a
whole book is a deliberate, occasional action with a real cost (a full
book's worth of tokens), not something that should be one ambiguous
voice command away.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.book_reader import extract_text
from companion.config import require_api_key
from companion.learning import LearningStore

# Generous but bounded - comfortably covers real books (a 200k-word novel
# is ~1.2M characters) while still catching a pathological extraction
# (garbled/duplicated PDF text) before it turns into a huge, expensive call.
MAX_CHARS = 3_000_000

SUMMARY_PROMPT = """This is the full text of a book. Write, in this exact format:

## Title
The book's actual title (infer from the text if not obviously stated at the top).

## Summary
A clear, structured summary covering the main ideas, arguments, or plot - 300-500 words. Organize around the book's own structure (chapters/sections/themes), not one flat paragraph.

## Key Takeaway
The single most important, actionable point from this book, in 1-2 sentences.

Base this only on the text below - don't add outside knowledge about the book or author beyond what's actually in it.

=== BOOK TEXT ===
{text}"""


def _parse_sections(raw: str) -> dict:
    def section(name: str, stop_names: list[str]) -> str:
        stop_pattern = "|".join(re.escape(s) for s in stop_names) or r"\Z"
        m = re.search(rf"## {re.escape(name)}\s*\n(.*?)(?=\n## (?:{stop_pattern})|\Z)", raw, re.S)
        return m.group(1).strip() if m else ""

    return {
        "title": section("Title", ["Summary", "Key Takeaway"]),
        "summary": section("Summary", ["Key Takeaway"]),
        "key_takeaway": section("Key Takeaway", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book_path", help="Path to a .pdf or .epub file")
    parser.add_argument("--topic", help="Override the auto-detected title")
    args = parser.parse_args()

    print(f"Reading {args.book_path}...")
    text = extract_text(args.book_path)
    if not text.strip():
        print("Got no extractable text out of that file - it might be scanned images (no OCR here) "
              "or DRM-protected.", file=sys.stderr)
        sys.exit(1)

    if len(text) > MAX_CHARS:
        print(f"  (extracted {len(text):,} chars, truncating to {MAX_CHARS:,} - unusually long for a single book)")
        text = text[:MAX_CHARS]
    else:
        print(f"  extracted {len(text):,} characters")

    print("Summarizing with Claude (this may take a bit for a full book)...")
    from anthropic import Anthropic

    client = Anthropic(api_key=require_api_key())
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": SUMMARY_PROMPT.format(text=text)}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    parsed = _parse_sections(raw)

    topic = args.topic or parsed["title"] or Path(args.book_path).stem
    summary = parsed["summary"] or raw  # fall back to the raw response if parsing somehow failed
    key_takeaway = parsed["key_takeaway"] or "(not extracted - see summary)"

    store = LearningStore()
    item = store.add(topic=topic, summary=summary, key_takeaway=key_takeaway)

    print(f"\nSaved: {item.topic}")
    print(f"Key takeaway: {item.key_takeaway}")
    print(f"First review scheduled: {item.next_review_at}")
    print(f"\nFull summary:\n{summary}")


if __name__ == "__main__":
    main()
