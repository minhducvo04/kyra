"""kyra_memory behaviour on a synthetic store: the pack Kyra gets, the inbox merge, supersede history, the index."""
import hashlib

import pytest

from kyra_memory import inbox
from kyra_memory.assembler import assemble_pack
from kyra_memory.labels import Tier

TASK = "deploy gate cache warm"
# sha256 of the pack text for this synthetic store and caller-supplied headers.
GOLDEN = {False: "e2be1b7c213b7e49bc2602c3dd920110e7193b3f89a52f78c9b3e3541c649560",
          True: "a60f5319e3cf0a79e65c3f875deec05e440bb3121319bc884e920d75be5db320"}


@pytest.mark.parametrize("lessons_first", [False, True])
def test_pack_is_byte_identical_to_the_synthetic_reference_pack(store, root, scope, kyra_headers, lessons_first):
    pack = assemble_pack(root, "builders", TASK, record=False, lessons_first=lessons_first,
                         scope=scope, headers=kyra_headers)
    assert hashlib.sha256(pack.text.encode()).hexdigest() == GOLDEN[lessons_first]


def test_headers_and_scope_are_parameters(store, root, scope, kyra_headers):
    pack = assemble_pack(root, "builders", TASK, record=False, scope=scope, headers={**kyra_headers, "rules": "## R"})
    assert pack.text.startswith("## R\n")
    with pytest.raises(FileNotFoundError):
        assemble_pack(root, "builders", TASK, record=False, scope="other")


def test_merge_takes_owner_pins_and_refuses_them_from_a_team(store, root):
    label = (Tier.T1, frozenset())
    inbox.append_add(root, "builders", "pinned", "sneaky pin", source="agent", label=label)
    inbox.append_add(root, "owner", "pinned", "real pin", source="owner", label=label)
    inbox.append(root, "builders", {"kind": "used", "task": "t", "items": [["facts", 1]]})
    report = inbox.merge(store, root)
    assert report.refused_owner_only == 1
    assert report.applied == {"add": 1, "used": 1}
    assert [r.body for r in store.search("pinned", "real pin")] == ["real pin"]
    assert store.search("pinned", "sneaky") == []


def test_a_correction_supersedes_and_keeps_the_history(store):
    old = store.search("facts", "retries")[0]
    new_id = store.add("facts", "Deploy retries are capped at five.", source="agent", key="retries",
                       supersedes=old.id, label=old.label)
    chain = store.history("facts", new_id)
    assert [r.status for r in chain] == ["superseded", "active"]
    assert old.id in [r.id for r in store.as_of("facts", old.knowledge_time)]


def test_reindex_takes_the_encoder_as_a_parameter(store, root, scope, monkeypatch):
    np = pytest.importorskip("numpy")
    from kyra_memory import vector_index
    from kyra_memory.vector_index import IndexedEmbedding, reindex

    monkeypatch.setattr(vector_index, "_default_encoder", None)  # a host that imported Kyra's alias installs one

    def encoder(texts, name, *, query=False):
        return np.ones((len(texts), 4), dtype="float32")

    with pytest.raises(ValueError, match="no encoder"):
        reindex(root, "m", scope=scope)
    total = sum(len(store.all_rows(t)) for t in ("rules", "pinned", "facts", "faults", "recipes"))
    assert reindex(root, "m", encoder=encoder, scope=scope) == total
    index = IndexedEmbedding(root, "m", encoder=encoder)
    assert len(index(["a text not in the index"])[0]) == 4  # unindexed text goes to the encoder
