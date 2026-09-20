"""The search_kyra_data chat tool.

The property worth pinning hardest is the one the web panel hands to Duc as a
switch and this tool must never hand to the model: private material. The panel
has a deliberate human gesture behind it; a chat turn has no such gesture, and
the far end of a chat turn is the Anthropic API. So the tool has no
include_sensitive parameter at all - not a default, an absence.
"""
import pytest

from companion.search import Chunk, Hit, SearchKyraDataTool


def _hit(cid="c1", path="docs/design.md", kind="doc", sensitive=False,
         text="RRF fuses ranked lists by 1/(60 + rank)", score=0.5):
    chunk = Chunk(chunk_id=cid, source_id="s1", path=path, kind=kind, title="Design",
                  index=0, text=text, sensitive=sensitive, mtime=0.0)
    return Hit(chunk, score, lexical_rank=1, vector_rank=2)


class FakeIndex:
    """Records the arguments the tool passed down, so a test can assert on what
    the tool asked for and not only on what it returned."""

    def __init__(self, hits=None, last_indexed=1_757_000_000.0):
        self.calls = []
        self._hits = hits if hits is not None else [_hit()]
        self._last = last_indexed

    def search(self, query, k=10, kinds=None, include_sensitive=False, **kw):
        self.calls.append({"query": query, "k": k, "kinds": kinds,
                           "include_sensitive": include_sensitive, **kw})
        return [h for h in self._hits if include_sensitive or not h.chunk.sensitive]

    def last_indexed(self):
        return self._last


def test_the_tool_definition_is_the_shape_claude_expects():
    tool = SearchKyraDataTool(index=FakeIndex())
    schema = tool.to_schema()
    assert schema["name"] == "search_kyra_data"
    assert schema["input_schema"]["required"] == ["query"]
    assert "query" in schema["input_schema"]["properties"]
    assert schema["description"].strip()


def test_it_returns_hits_the_model_can_cite():
    index = FakeIndex()
    out = SearchKyraDataTool(index=index).run(query="why k=60")
    assert index.calls[0]["query"] == "why k=60"
    assert out["results"][0]["path"] == "docs/design.md"
    assert out["results"][0]["kind"] == "doc"
    assert "RRF fuses" in out["results"][0]["text"]


def test_private_material_is_never_searched_for_at_all():
    # Not filtered out of the results afterwards - never asked for. The call the
    # tool makes is the thing under test.
    index = FakeIndex(hits=[_hit(cid="p1", path="data/private_docs/needs-your-input.md",
                                 kind="private", sensitive=True, text="salary target"),
                            _hit()])
    out = SearchKyraDataTool(index=index).run(query="salary")
    assert index.calls[0]["include_sensitive"] is False
    assert all(r["kind"] != "private" for r in out["results"])
    assert "salary target" not in str(out)


def test_asking_for_the_private_kind_is_refused_rather_than_quietly_emptied():
    # A refusal tells the model (and Duc, reading the transcript) that the
    # material exists and is off limits; an empty list would read as "nothing
    # there", which is a different and false statement.
    out = SearchKyraDataTool(index=FakeIndex()).run(query="salary", kind="private")
    assert "private" in out["error"].lower()


def test_a_kind_filter_is_passed_through():
    index = FakeIndex()
    SearchKyraDataTool(index=index).run(query="northwind", kind="resume")
    assert index.calls[0]["kinds"] == ["resume"]


def test_an_unknown_kind_is_reported_not_raised():
    out = SearchKyraDataTool(index=FakeIndex()).run(query="x", kind="nonsense")
    assert "nonsense" in out["error"]


def test_an_empty_query_is_reported_not_raised():
    assert "error" in SearchKyraDataTool(index=FakeIndex()).run(query="   ")


def test_k_is_clamped_to_something_a_prompt_can_hold():
    index = FakeIndex()
    SearchKyraDataTool(index=index).run(query="x", k=500)
    assert index.calls[0]["k"] <= 10


def test_it_says_how_old_the_index_is_because_search_never_reindexes():
    # There is no staleness check on search (CLAUDE.md): a query right after an
    # edit answers from the previous index. Reporting the date is what stops
    # that from being a silent wrong answer.
    out = SearchKyraDataTool(index=FakeIndex()).run(query="x")
    assert out["index_last_updated"].startswith("2025-09-04")  # 1_757_000_000 in local time
    assert SearchKyraDataTool(index=FakeIndex(last_indexed=None)).run(query="x")["index_last_updated"] == "never"


def test_building_the_tool_does_not_open_the_index():
    # default_tool_registry() runs at import time in all three front doors; a
    # tool that opened SQLite and Chroma there would cost every startup.
    opened = []

    class Boom:
        def __init__(self):
            opened.append(True)

    tool = SearchKyraDataTool(index_factory=Boom)
    assert not opened
    with pytest.raises(AttributeError):
        tool.run(query="x")  # the factory only runs now
    assert opened == [True]
