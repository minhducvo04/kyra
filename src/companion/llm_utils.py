"""Small shared helpers for working with Claude API responses."""


def extract_text(response) -> str:
    """Pull the reply text out of a Messages API response.

    response.content is a list of blocks. With extended thinking on, a
    ThinkingBlock can come before the TextBlock, so content[0] isn't
    reliably the reply - this finds the actual text block(s) instead.
    """
    return "".join(block.text for block in response.content if block.type == "text")
