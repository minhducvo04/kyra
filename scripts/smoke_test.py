"""Quick smoke test: confirms the API key works and the package is wired up."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.config import require_api_key
from companion.llm import build_anthropic_client
from companion.llm_utils import extract_text
from companion.privacy import Tier
from companion.provider import release_label


def main() -> None:
    client = build_anthropic_client(api_key=require_api_key())
    with release_label(Tier.T0, frozenset()):
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=100,
            messages=[
                {
                    "role": "user",
                    "content": "Say hello in one short sentence, as if you were a friendly AI companion greeting your creator for the first time.",
                }
            ],
        )
    print(extract_text(response))


if __name__ == "__main__":
    main()
