from companion.job_applications import JobApplicationStore
from companion.job_posting_fetch import TargetPostingTool, fetch_posting, parse_posting_url

GH = {"company_name": "Meridian", "title": "Software Engineer, New Grad", "absolute_url": "https://job-boards.greenhouse.io/janestreet/jobs/1",
      "first_published": "2026-08-19T17:15:50-04:00", "location": {"name": "New York"},
      "content": "&lt;p&gt;Join the Trading Systems team.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;C++&lt;/li&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"}
LV = {"text": "Software Engineer, New Grad", "hostedUrl": "https://jobs.lever.co/palantir/94984771-0704-446c-88c6-91ce748f6d92",
      "openingPlain": "We build software for institutions.", "lists": [{"text": "What we value", "content": "<li>Ownership</li>"}],
      "additionalPlain": "Must be authorized to work in the US.", "createdAt": 1756728000000, "categories": {"location": "New York, NY"}}
AB = {"jobs": [{"id": "d9bcb6a2-0e54-4cb3-baec-43f2d74db18f", "title": "Software Engineer (Agent Platform) - New Grad",
                "descriptionPlain": "Join the Agent Platform team. Requirements\n- Python\n- TypeScript", "publishedAt": "2026-08-01T00:00:00+00:00",
                "jobUrl": "https://jobs.ashbyhq.com/netic/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f", "location": "San Francisco"}]}


def _fake(url):
    if "greenhouse" in url:
        return GH
    if "lever" in url:
        return LV
    return AB


def test_parse_and_fetch_three_boards():
    assert parse_posting_url("https://job-boards.greenhouse.io/janestreet/jobs/1") == ("greenhouse", "janestreet", "1")
    assert parse_posting_url("https://www.linkedin.com/jobs/view/4438446984/") is None
    gh = fetch_posting("https://job-boards.greenhouse.io/janestreet/jobs/1", _fake)
    assert gh.company == "Meridian" and "Join the Trading Systems team." in gh.text and "- C++" in gh.text and "<" not in gh.text
    lv = fetch_posting("https://jobs.lever.co/palantir/94984771-0704-446c-88c6-91ce748f6d92", _fake)
    assert lv.title.startswith("Software Engineer") and "Ownership" in lv.text and "authorized" in lv.text and lv.posted_at
    ab = fetch_posting("https://jobs.ashbyhq.com/netic/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f", _fake)
    assert ab.company == "netic" and ab.text.startswith("Join the Agent Platform team")


def test_target_tool_logs_targeting_entry_with_signals_and_dedups(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    tool = TargetPostingTool(store, fetch=lambda url: fetch_posting(url, _fake))
    out = tool.run(url="https://job-boards.greenhouse.io/janestreet/jobs/1")
    assert out["created"] and out["application"]["status"] == "targeting" and out["application"]["company"] == "Meridian"
    assert out["signals"].startswith("Priority") and "Team named: Trading Systems" in out["signals"]
    assert store.list()[0].notes.startswith("[signals]")
    again = tool.run(url="https://job-boards.greenhouse.io/janestreet/jobs/1")
    assert not again["created"] and len(store.list()) == 1


def test_target_tool_needs_text_for_unknown_sites(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    tool = TargetPostingTool(store)
    assert "error" in tool.run(url="https://www.linkedin.com/jobs/view/4438446984/")
    out = tool.run(url="https://www.linkedin.com/jobs/view/4438446984/", posting_text="Join the Agent Platform team. Python.",
                   company="Netic", role="Software Engineer (Agent Platform) - New Grad")
    assert out["created"] and out["application"]["link"].endswith("/4438446984/")
    assert "error" in tool.run(url="https://example.com/job", posting_text="text but no company")
