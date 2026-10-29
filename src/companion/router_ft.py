"""Prompts for an optional fine-tuned TurnRouter classifier.

The private repository also holds the fine-tune pipeline (synthetic data,
LoRA training and evaluation against a handwritten held-out set). This public
package keeps only the two short system prompts that router.py and its tests
use when a fine-tuned adapter is configured.
"""

# The prompt the fine-tuned model sees. Short on purpose - the few-shot
# examples and tool descriptions that make router.CLASSIFIER_PROMPT long
# are what the adapter is meant to have learned.
COMPACT_SYSTEM = (
    "Classify the user's message for an assistant router. Reply with JSON only: "
    '{"path": "tool"|"text", "backend": "claude"|"local"}. '
    "tool = it asks for a reminder, tech news, science facts, saving/reviewing learning items, "
    "saving a durable fact about the user, "
    "looking something up in the user's own saved material (what was decided, written or noted), "
    "reading or changing the room humidifier (humidity, mist, display, night light), or asking what to work on next. "
    "text = anything else; backend claude for explanations, careful reasoning, or work coordination, local for casual chat."
)

# Backend-only policy; the primary prompt above stays unchanged.
BACKEND_SYSTEM = (
    'Classify the user\'s message for an assistant router. Reply with JSON only: {"path": "tool"|"text", '
    '"backend": "claude"|"local"}. tool = it asks for a reminder, tech news, science facts, '
    "saving/reviewing learning items, saving a "
    "durable fact about the user, looking something up in the user's own saved material (what was decided, "
    'written or noted), reading or changing the room humidifier (humidity, mist, display, night light), or '
    'asking what to work on next. text = anything else; backend local for casual chat, simple stable facts,'
    ' brief definitions, fixed conversions, spelling, small arithmetic or a one-command general computing '
    'how-to; claude for depth, advice, writing, synthesis, personal context or work coordination.'
)
