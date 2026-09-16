"""The CONSOLE: every tool, every agent thread and every run in one panel.

Plan: docs/plans/2026-09-13-console.md. Red on the commit that adds this file.

Two of these are hard constraints (AGENTS.md section 5): a tool that leaves the
machine, spends tokens or opens a browser is never run from a button without a
confirmation the *server* checks; and every tool run is recorded whoever asked
for it. The rest pins the HTTP contracts the panel is built on, so the browser
work and the backend work can proceed against the same shape.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from companion import session_log
from companion.tools import Tool, ToolRegistry


@pytest.fixture(scope="module")
def client():
    import companion.webapp as webapp

    with TestClient(webapp.app) as c:
        yield c


class _Echo(Tool):
    name = "echo"
    description = "Returns its input."
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def run(self, text: str) -> dict:
        return {"echo": text}


class _Boom(Tool):
    name = "boom"
    description = "Always fails."
    input_schema = {"type": "object", "properties": {}}

    def run(self) -> dict:
        raise RuntimeError("kaboom")


def _store(tmp_path):
    from companion.tool_runs import ToolRunStore

    eng = create_engine(f"sqlite:///{tmp_path / 'runs.db'}", connect_args={"check_same_thread": False})
    return ToolRunStore(eng)


# ---- the audit hook -------------------------------------------------------


def test_a_tool_needs_no_confirmation_unless_it_says_so():
    assert Tool.needs_confirmation is False
    assert _Echo().needs_confirmation is False


def test_registry_records_every_run_when_a_store_is_attached(tmp_path):
    store = _store(tmp_path)
    reg = ToolRegistry([_Echo(), _Boom()], audit=store)
    assert reg.run("echo", text="hi") == {"echo": "hi"}
    with pytest.raises(RuntimeError):
        reg.run("boom")
    runs = store.list()
    assert [r.tool for r in runs] == ["boom", "echo"]  # newest first
    ok, failed = runs[1], runs[0]
    assert ok.ok is True and ok.args == {"text": "hi"} and "hi" in ok.summary and ok.error is None
    assert failed.ok is False and "kaboom" in failed.error
    assert ok.started_at and ok.duration_ms >= 0


def test_registry_without_a_store_still_runs(tmp_path):
    assert ToolRegistry([_Echo()]).run("echo", text="x") == {"echo": "x"}


def test_a_broken_audit_store_never_breaks_the_tool(tmp_path):
    class Broken:
        def record(self, *a, **k):
            raise OSError("disk full")

    reg = ToolRegistry([_Echo()], audit=Broken())
    assert reg.run("echo", text="still works") == {"echo": "still works"}


def test_store_summary_is_bounded_and_list_honours_limit(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.record("echo", {"i": i}, ok=True, summary="x" * 1000, error=None, duration_ms=1)
    runs = store.list(limit=3)
    assert len(runs) == 3 and runs[0].args == {"i": 4}
    assert all(len(r.summary) <= 300 for r in runs)


def test_default_registry_records_to_the_shared_store():
    """Chat, voice, the digest and the console must all land in the same list."""
    from companion.default_tools import default_tool_registry
    from companion.tool_runs import ToolRunStore

    reg = default_tool_registry()
    assert isinstance(reg.audit, ToolRunStore)


@pytest.mark.parametrize(
    "name",
    ["autofill_job_application", "draft_application_material", "draft_outreach_note", "copy_outreach_note", "suggest_initiatives"],
)
def test_tools_that_leave_the_machine_or_spend_tokens_need_confirmation(name):
    from companion.default_tools import default_tool_registry

    tool = next(t for t in default_tool_registry() if t.name == name)
    assert tool.needs_confirmation is True, name


def test_local_read_only_tools_do_not_need_confirmation():
    from companion.default_tools import default_tool_registry

    reg = default_tool_registry()
    for name in ("list_reminders", "add_reminder", "focus_status", "list_outreach"):
        assert next(t for t in reg if t.name == name).needs_confirmation is False, name


# ---- the job queue --------------------------------------------------------


def test_job_queue_lists_newest_first(tmp_path):
    from companion.jobs import DbJobQueue
    from companion.schema import metadata

    eng = create_engine(f"sqlite:///{tmp_path / 'q.db'}", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    q = DbJobQueue(eng)
    first = q.enqueue("a", {"secret": 1})
    second = q.enqueue("b", {})
    jobs = q.list(limit=10)
    assert [j.id for j in jobs] == [second, first]
    assert q.list(limit=1)[0].id == second


# ---- the hand-off thread summary -----------------------------------------


def test_summarize_reads_the_last_block():
    session_log.append("claude", "tests", "older", next_up="old next", suggest="old model", branch="console-t1")
    session_log.append("codex", "build", "newer", next_up="write the panel", suggest="Codex / medium", branch="console-t1")
    s = session_log.summarize(session_log.read("console-t1"))
    assert s["agent"] == "codex" and s["open_for"] == "build"
    assert s["next"] == "write the panel" and s["suggested"] == "Codex / medium"
    assert s["stamp"]


def test_summarize_of_an_empty_thread_is_honest():
    s = session_log.summarize("")
    assert s["agent"] is None and s["open_for"] is None


# ---- HTTP: tools ----------------------------------------------------------


def test_tools_endpoint_lists_the_whole_registry(client):
    import companion.webapp as webapp

    body = client.get("/api/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert names == {t.name for t in webapp._registry}
    row = next(t for t in body["tools"] if t["name"] == "add_reminder")
    assert row["input_schema"]["type"] == "object"
    assert row["needs_confirmation"] is False
    assert row["group"] == "reminders"
    assert row["panel"] == "tools/reminders"
    for t in body["tools"]:
        assert t["panel"] is None or t["panel"].count("/") == 1


def test_tools_run_executes_a_local_tool_and_records_it(client):
    res = client.post("/api/tools/add_reminder/run", json={"input": {"text": "console test", "due_at": "2030-01-01T00:00:00+00:00"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tool"] == "add_reminder" and body["run_id"]
    runs = client.get("/api/tools/runs").json()["runs"]
    assert runs[0]["id"] == body["run_id"] and runs[0]["tool"] == "add_reminder" and runs[0]["ok"] is True
    assert any(r["text"] == "console test" for r in client.get("/api/reminders").json()["reminders"])


def test_tools_run_refuses_a_guarded_tool_without_confirmation(client):
    res = client.post("/api/tools/draft_outreach_note/run", json={"input": {"contact_id": 1}})
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "confirmation_required" and err["details"]["tool"] == "draft_outreach_note"
    # And nothing ran: no run row for it.
    assert not any(r["tool"] == "draft_outreach_note" for r in client.get("/api/tools/runs").json()["runs"])


def test_tools_run_unknown_tool_is_404(client):
    res = client.post("/api/tools/nope/run", json={"input": {}})
    assert res.status_code == 404 and res.json()["error"]["code"] == "unknown_tool"


def test_tools_run_bad_input_is_400_not_500(client):
    res = client.post("/api/tools/add_reminder/run", json={"input": {"bogus": 1}})
    assert res.status_code == 400 and res.json()["error"]["code"] == "tool_input_invalid"


def test_tools_run_turns_a_tool_error_dict_into_400(client):
    # update_outreach_status on a missing contact answers {"error": ...} to a model; HTTP must not say 200.
    res = client.post("/api/tools/update_outreach_status/run", json={"input": {"id": 999999, "status": "sent"}})
    assert res.status_code == 400 and res.json()["error"]["code"] == "tool_error"


def test_tool_runs_endpoint_honours_limit(client):
    for _ in range(3):
        client.post("/api/tools/list_reminders/run", json={"input": {}})
    assert len(client.get("/api/tools/runs?limit=2").json()["runs"]) == 2


# ---- HTTP: jobs and agents -------------------------------------------------


def test_jobs_list_never_returns_payload_or_result(client, monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    import companion.webapp as webapp
    from companion.jobs import DbJobQueue
    from companion.schema import metadata

    # A private queue: a demo job left in the shared one would be claimed by later tests' worker runs.
    eng = create_engine(f"sqlite:///{tmp_path / 'q.db'}", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    monkeypatch.setattr(webapp, "_queue", DbJobQueue(eng))
    webapp._queue.enqueue("demo", {"resume_text": "PRIVATE"})
    body = client.get("/api/jobs?limit=5").json()
    assert body["jobs"] and body["jobs"][0]["kind"] == "demo"
    assert "PRIVATE" not in json.dumps(body)
    assert set(body["jobs"][0]) == {"id", "kind", "status", "created_at", "started_at", "finished_at", "error"}


def test_agents_endpoint_lists_threads_newest_first_with_repo_facts(client):
    session_log.append("claude", "duc", "waiting on a decision", next_up="Duc decides", suggest="n/a", branch="console-web")
    body = client.get("/api/agents").json()
    assert set(body["repo"]) >= {"branch", "head", "subject", "tree"}
    row = next(t for t in body["threads"] if t["slug"] == "console-web")
    assert row["agent"] == "claude" and row["open_for"] == "duc" and row["next"] == "Duc decides"
    assert row["modified"]


def test_agent_thread_text_and_not_found(client):
    session_log.append("codex", "review", "the body text", branch="console-read")
    body = client.get("/api/agents/console-read").json()
    assert body["slug"] == "console-read" and "the body text" in body["text"]
    res = client.get("/api/agents/does-not-exist")
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"


def test_agent_thread_slug_cannot_escape_the_sessions_dir(client):
    res = client.get("/api/agents/..%2F..%2F.env")
    assert res.status_code == 404


# ---- the HUD markup ----------------------------------------------------------


def test_index_has_the_console_panel_and_toggle(client):
    html = client.get("/").text
    assert 'id="console-toggle"' in html and 'id="console-panel"' in html
    for tab in ("tools", "agents", "runs"):
        assert f'data-console-tab="{tab}"' in html and f'data-console-tab-panel="{tab}"' in html
