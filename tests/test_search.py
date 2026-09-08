"""Post-conditions for the search index.

The sensitivity test is the important one: `data/private_docs/` holds
interview prep and job-search notes, and this repo has already turned one
true-but-private fact into a fabricated resume entry. "Don't use private
notes" is a prompt request; a default argument with a test behind it is a
guarantee.
"""
import pytest

from companion.search import (
    MAX_CHUNK_WORDS,
    FileSource,
    HybridSearchIndex,
    answer,
    chunk_text,
    enforce_citations,
    fts_match_query,
    rrf_fuse,
)
from tests.fakes import HashingEmbedding, ScriptedLLM


@pytest.fixture
def corpus(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir()
    (public / "embeddings.md").write_text(
        "# Why BGE\n\nWe picked BGE over MiniLM because the retrieval scores are better.\n"
        "\n# Recency\n\nRetrieval blends similarity with RECENCY_WEIGHT so today stays boosted.\n",
        encoding="utf-8",
    )
    (public / "voice.md").write_text(
        "# Voice\n\nKokoro and faster-whisper run locally so a turn costs nothing.\n",
        encoding="utf-8",
    )
    (private / "prep.md").write_text(
        "# Interview prep\n\nThe salary anchor to use is a number Duc has not shared publicly.\n",
        encoding="utf-8",
    )
    return public, private


@pytest.fixture
def index(tmp_path, corpus):
    public, private = corpus
    idx = HybridSearchIndex(dir_path=tmp_path / "index", embedding_function=HashingEmbedding())
    idx.index([
        FileSource(public, ["*.md"], kind="doc"),
        FileSource(private, ["*.md"], kind="private", sensitive=True),
    ])
    yield idx
    idx.close()


def _sources(index, *args, **kwargs):
    return {hit.chunk.path for hit in index.search(*args, **kwargs)}


# -- chunking ---------------------------------------------------------------


def test_no_chunk_exceeds_the_embedding_window():
    # bge-small truncates past 512 tokens; a chunk over the word cap would be
    # silently cut and the tail would never be searchable.
    text = " ".join(f"word{i}" for i in range(5000))
    assert all(len(c.split()) <= MAX_CHUNK_WORDS for c in chunk_text(text))


def test_oversized_blocks_overlap_so_a_sentence_is_never_split_away():
    chunks = chunk_text(" ".join(f"w{i}" for i in range(MAX_CHUNK_WORDS * 2)))
    assert len(chunks) >= 2
    first, second = chunks[0].split(), chunks[1].split()
    assert set(first) & set(second), "consecutive windows must overlap"


def test_markdown_splits_on_headings_and_packs_small_sections():
    text = "# A\n\nalpha\n\n# B\n\nbeta\n"
    # Both sections are tiny, so they pack into one chunk rather than
    # producing two chunks that are mostly heading.
    assert chunk_text(text, "markdown") == ["# A\n\nalpha\n\n# B\n\nbeta"]


# -- fusion and query handling ----------------------------------------------


def test_rrf_rewards_agreement_over_a_single_top_rank():
    # Ranked first by one retriever only, versus second by both.
    scores = rrf_fuse([["solo", "both"], ["other", "both"]])
    assert scores["both"] > scores["solo"]


def test_fts_match_query_reduces_operators_to_plain_tokens():
    # Quotes, NEAR, * and - are FTS5 syntax; a raw query is a syntax error.
    assert fts_match_query('"one-page" NEAR resume*') == '"one" OR "page" OR "NEAR" OR "resume"'
    assert fts_match_query("!!!") == ""


def test_a_punctuated_natural_question_does_not_raise(index):
    assert index.search('what\'s the "one-page" resume loop -- and why?') is not None


# -- retrieval --------------------------------------------------------------


def test_finds_a_document_by_exact_token(index):
    assert "embeddings.md" in " ".join(_sources(index, "RECENCY_WEIGHT"))


def test_finds_a_document_by_meaning_not_shared_words(index):
    hits = index.search("Kokoro faster-whisper local", mode="vector")
    assert hits and any("voice.md" in h.chunk.path for h in hits)


def test_unknown_kind_is_an_error_not_an_empty_result(index):
    with pytest.raises(ValueError, match="unknown kind"):
        index.search("anything", kinds=["nonsense"])


# -- the sensitivity post-condition -----------------------------------------


def test_private_documents_never_surface_by_default(index):
    # Query the private doc's own words: it must still not come back.
    for mode in ("hybrid", "lexical", "vector"):
        hits = index.search("salary anchor Duc has not shared publicly", mode=mode)
        assert not any(h.chunk.sensitive for h in hits), f"{mode} leaked a sensitive chunk"


def test_private_documents_are_searchable_when_explicitly_asked_for(index):
    hits = index.search("salary anchor", include_sensitive=True)
    assert any(h.chunk.sensitive for h in hits)


# -- incremental indexing ---------------------------------------------------


def test_reindexing_unchanged_files_re_embeds_nothing(index, corpus):
    public, private = corpus
    stats = index.index([
        FileSource(public, ["*.md"], kind="doc"),
        FileSource(private, ["*.md"], kind="private", sensitive=True),
    ])
    assert (stats.added, stats.updated, stats.chunks) == (0, 0, 0)
    assert stats.unchanged == 3


def test_editing_one_file_updates_only_that_source(index, corpus):
    public, private = corpus
    (public / "voice.md").write_text("# Voice\n\nNow it mentions Silero as well.\n", encoding="utf-8")
    stats = index.index([
        FileSource(public, ["*.md"], kind="doc"),
        FileSource(private, ["*.md"], kind="private", sensitive=True),
    ])
    assert (stats.added, stats.updated, stats.unchanged, stats.deleted) == (0, 1, 2, 0)
    assert "Silero" in " ".join(h.chunk.text for h in index.search("Silero"))


def test_an_edited_document_replaces_its_vector_not_only_its_text(index, corpus):
    """The test above searches hybrid, which the lexical half alone can
    satisfy - so a stale embedding would pass it unnoticed. Chunk ids are
    deterministic (`source_id#i`), so re-indexing rewrites ids that already
    exist, and Chroma ignores an add whose id is taken. `_drop_source`'s
    vector delete frees them first, but it is deliberately swallowed on
    failure; sabotaging it here proves the write does not depend on it.
    """
    public, _ = corpus
    sources = [FileSource(public, ["*.md"], kind="doc")]

    def vector_text() -> str:
        rows = index._vectors().get(include=["documents"])
        return " ".join(rows["documents"])

    (public / "voice.md").write_text("# Voice\n\nNow it mentions Silero.\n", encoding="utf-8")
    index.index(sources)
    assert "Silero" in vector_text()

    def refuse(*args, **kwargs):
        raise RuntimeError("simulated vector delete failure")

    index._vectors().delete = refuse
    (public / "voice.md").write_text("# Voice\n\nAnd now Whisper too.\n", encoding="utf-8")
    index.index(sources)
    assert "Whisper" in vector_text()


def test_a_deleted_file_leaves_no_hits_behind(index, corpus):
    public, private = corpus
    (public / "voice.md").unlink()
    stats = index.index([
        FileSource(public, ["*.md"], kind="doc"),
        FileSource(private, ["*.md"], kind="private", sensitive=True),
    ])
    assert stats.deleted == 1
    assert not any("voice.md" in p for p in _sources(index, "Kokoro"))


# -- housekeeping -----------------------------------------------------------


def test_inventory_reports_files_it_holds_but_cannot_search(index, corpus):
    # The storage-management question is "what does Kyra have that search
    # can't see" - which includes types no pattern claims, not only
    # unreadable ones.
    public, _ = corpus
    (public / "resume.pdf").write_bytes(b"%PDF-1.4 not really")
    report = index.inventory([FileSource(public, ["*.md"], kind="doc")])
    assert any(p.endswith("resume.pdf") for p in report["orphans"])
    assert report["orphans_by_ext"][".pdf"] == 1
    assert report["chunks"] > 0


def test_inventory_does_not_list_a_directory_the_source_only_borrows(index, corpus):
    # FileSource(PROJECT_ROOT, ["CLAUDE.md"]) must not report the whole repo.
    public, _ = corpus
    (public / "unrelated.rtf").write_text("not ours", encoding="utf-8")
    report = index.inventory([FileSource(public, ["*.md"], kind="doc", owns_root=False)])
    assert report["orphans"] == []


# -- answering --------------------------------------------------------------


def test_invented_citations_are_removed_and_reported():
    # A citation pointing at nothing reads as provenance while providing none.
    text, used, warnings = enforce_citations("Kokoro is local [1]. It costs nothing [9].", 3)
    assert "[9]" not in text
    assert used == [1]
    assert any("never retrieved" in w for w in warnings)


def test_an_uncited_answer_is_flagged_rather_than_trusted():
    _, used, warnings = enforce_citations("It runs locally.", 3)
    assert used == []
    assert any("unsourced" in w for w in warnings)


def test_the_model_is_never_shown_a_private_document(index):
    # The generation path inherits the same gate as the result list: a
    # private note must not reach a draft through the back door of an answer.
    llm = ScriptedLLM(["Nothing relevant [1]."])
    answer(index, "salary anchor Duc has not shared publicly", llm=llm)
    assert llm.calls, "the model should have been asked"
    prompt = llm.calls[0]["user_input"]
    # The query itself is in the prompt, of course; what must not be there is
    # the private file or any line of it.
    assert "prep.md" not in prompt
    assert "The salary anchor to use is" not in prompt


def test_answer_resolves_its_citations_to_real_sources(index):
    llm = ScriptedLLM(["BGE beat MiniLM on retrieval [1]."])
    result = answer(index, "why BGE over MiniLM", llm=llm)
    assert result.warnings == []
    assert len(result.citations) == 1
    assert result.citations[0].path == result.hits[0].chunk.path
