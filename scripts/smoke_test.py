"""Quick smoke test: confirms the API key works and the package is wired up."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anthropic import Anthropic

from companion.config import require_api_key


def main() -> None:
    client = Anthropic(api_key=require_api_key())
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
    print(response.content[0].text)


if __name__ == "__main__":
    main()
