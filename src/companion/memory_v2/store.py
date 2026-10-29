"""Alias of kyra_memory.store: old importers and their monkeypatches see the package's own objects."""
import sys

from kyra_memory import store as _impl

_impl.DEFAULT_SCOPE = "kyra"  # Kyra's one scope; the package itself defaults to a neutral name
sys.modules[__name__] = _impl
