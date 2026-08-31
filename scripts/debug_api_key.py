"""
Standalone debug script for the Anthropic API key.

Run it yourself any time with:
    .venv/bin/python scripts/debug_api_key.py

Unlike smoke_test.py, this prints the *actual* error Anthropic's API sends
back (HTTP status + error body), not just the generic exception name -
that's usually where the real reason lives.
"""
import os

from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("ANTHROPIC_API_KEY", "")

print("=" * 60)
print("KEY CHECK (masked, safe to read)")
print("=" * 60)
print("found:", bool(api_key))
print("length:", len(api_key))
print("starts with sk-ant-:", api_key.startswith("sk-ant-"))
print("has surrounding whitespace:", api_key != api_key.strip())
if len(api_key) > 14:
    print("preview:", api_key[:10] + "..." + api_key[-4:])
print()

if not api_key:
    print("No key loaded at all - check .env exists next to this repo and has")
    print("a line like: ANTHROPIC_API_KEY=sk-ant-...")
    raise SystemExit(1)

print("=" * 60)
print("LIVE API CALL")
print("=" * 60)

from anthropic import Anthropic, APIStatusError

client = Anthropic(api_key=api_key)

try:
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=50,
        messages=[{"role": "user", "content": "Say hi in 5 words or less."}],
    )
    # response.content is a list of blocks - with extended thinking on, a
    # ThinkingBlock can come before the TextBlock, so this filters for the
    # actual text block(s) instead of assuming content[0] is the reply.
    reply_text = "".join(b.text for b in response.content if b.type == "text")
    print("SUCCESS")
    print(reply_text)

except APIStatusError as e:
    print(f"FAILED - HTTP {e.status_code} ({type(e).__name__})")
    print()
    print("This is the part that actually matters - the real reason from")
    print("Anthropic's API, not just the generic exception name:")
    print(e.body)

except Exception as e:
    print(f"FAILED - {type(e).__name__}: {e}")
