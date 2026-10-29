"""Alias of kyra_memory.assembler with Kyra's scope and pack headers installed as its defaults.

Old importers and their monkeypatches see the package's own objects; `wiring.pack_for` also passes both values
explicitly, so a pack built through Kyra is the one it always was.
"""
import sys

import companion.memory_v2.store  # noqa: F401  (installs Kyra's default scope)
from kyra_memory import assembler as _impl

KYRA_SCOPE = "kyra"
KYRA_HEADERS = {"rules": "## Rules", "pinned": "## Pinned (from the owner)", "hot": "## Often used",
                "facts": "## Facts", "faults": "## Known faults", "recipes": "## Recipe"}

_impl.DEFAULT_HEADERS = KYRA_HEADERS
_impl.KYRA_SCOPE = KYRA_SCOPE
_impl.KYRA_HEADERS = KYRA_HEADERS
sys.modules[__name__] = _impl
