"""Alias of kyra_memory.vector_index with Kyra's embedding model installed as its default encoder."""
import sys

import companion.memory_v2.store  # noqa: F401  (installs Kyra's default scope)
from companion.memory_v2 import embeddings
from kyra_memory import vector_index as _impl


def kyra_encoder(texts, name, *, query=False):
    return embeddings.encode(texts, name, query=query)


_impl.set_default_encoder(kyra_encoder)
_impl.kyra_encoder = kyra_encoder
sys.modules[__name__] = _impl
