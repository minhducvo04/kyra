"""A persistent library of background material for job-application
drafting - multiple resumes, style samples, and free-text notes Duc
adds directly, all remembered across sessions instead of the old
one-shot-upload-per-draft flow.

Separate from memory_notes.py on purpose, same reasoning as that
module's split from ChromaMemoryStore: this is job-application-
specific reference material (a resume version, a style sample, "here's
a summary of my last role"), selected explicitly per draft rather than
loaded in full every conversational turn. memory_notes.py stays the
"Kyra always knows this about Duc in any conversation" layer;
job_documents.py is "material available when drafting application
material," pulled in only when asked for.

Storage is a single JSON index (data/job_documents/index.json) holding
every document's extracted text inline - resumes/notes are small
enough that this stays simple and human-readable, no need for the
DB-per-row machinery job_applications.db uses for a much larger,
queried-by-status table.
"""
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "job_documents"
INDEX_NAME = "index.json"

VALID_KINDS = {"resume", "style_sample", "note"}


@dataclass
class JobDocument:
    id: str
    label: str
    kind: str  # "resume" | "style_sample" | "note"
    text: str
    source_filename: str | None
    added_at: str
    file_path: str | None = None  # set only when the original file bytes were kept (see add(file_bytes=...))


class JobDocumentStore:
    def __init__(self, dir_path: Path | str = DEFAULT_DIR):
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / INDEX_NAME
        self._files_dir = self._dir / "files"

    def _read_all(self) -> list[dict]:
        if not self._index_path.exists():
            return []
        docs = json.loads(self._index_path.read_text(encoding="utf-8"))
        for d in docs:
            d.setdefault("file_path", None)  # tolerate records saved before this field existed
        return docs

    def _write_all(self, docs: list[dict]) -> None:
        self._index_path.write_text(json.dumps(docs, indent=2), encoding="utf-8")

    def add(
        self,
        label: str,
        kind: str,
        text: str,
        source_filename: str | None = None,
        file_bytes: bytes | None = None,
    ) -> JobDocument:
        """file_bytes: the original uploaded file, kept on disk alongside its
        extracted text - needed for resumes specifically, since job_autofill.py
        attaches an actual file to a browser file input (extracted text alone
        can't be uploaded to a form). Optional because pasted-text documents
        (notes, or style samples typed directly) have no original file.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")
        text = text.strip()
        if not text:
            raise ValueError("document text can't be empty")
        doc_id = uuid.uuid4().hex[:12]

        file_path = None
        if file_bytes:
            suffix = Path(source_filename).suffix if source_filename else ""
            self._files_dir.mkdir(parents=True, exist_ok=True)
            saved_path = self._files_dir / f"{doc_id}{suffix}"
            saved_path.write_bytes(file_bytes)
            file_path = str(saved_path)

        doc = JobDocument(
            id=doc_id,
            label=label.strip() or (source_filename or "untitled"),
            kind=kind,
            text=text,
            source_filename=source_filename,
            added_at=datetime.now().astimezone().isoformat(),
            file_path=file_path,
        )
        docs = self._read_all()
        docs.append(asdict(doc))
        self._write_all(docs)
        return doc

    def list(self) -> "list[JobDocument]":
        return [JobDocument(**d) for d in self._read_all()]

    def get_many(self, ids: "list[str]") -> "list[JobDocument]":
        wanted = set(ids)
        return [JobDocument(**d) for d in self._read_all() if d["id"] in wanted]

    def delete(self, doc_id: str) -> bool:
        docs = self._read_all()
        removed = [d for d in docs if d["id"] == doc_id]
        remaining = [d for d in docs if d["id"] != doc_id]
        if not removed:
            return False
        self._write_all(remaining)
        file_path = removed[0].get("file_path")
        if file_path:
            Path(file_path).unlink(missing_ok=True)  # best-effort - don't fail the delete over a missing file
        return True
