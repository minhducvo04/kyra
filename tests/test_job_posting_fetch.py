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


WD = {"jobPostingInfo": {"title": "Software Engineer, New Grad",
                        "jobDescription": "&lt;p&gt;Build accelerated computing.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;C++&lt;/li&gt;&lt;/ul&gt;",
                        "startDate": "2026-09-01", "location": "US, CA, Santa Clara",
                        "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SWE_JR1"},
      # The legal entity, not what anyone calls the company - which is why it is not used.
      "hiringOrganization": {"name": "2100 NVIDIA USA"}}


def _fake(url):
    if "myworkdayjobs" in url:
        return WD
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
    # Ashby returns only the board slug; the fetcher titles it (or takes the watchlist
    # name) because this string becomes the tailored resume's filename.
    assert ab.company == "Netic" and ab.text.startswith("Join the Agent Platform team")


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


def test_company_name_comes_from_the_watchlist_not_the_board_slug(tmp_path):
    """Ashby and Lever return only the slug, and that string becomes the tracker's
    company and the tailored resume's FILENAME - "retell-ai" would reach an employer."""
    import json

    from companion.job_boards import company_for_token, titleize_token

    wl = tmp_path / "watchlist.json"
    wl.write_text(json.dumps([
        {"company": "Retell", "source": "ashby", "token": "retell-ai", "title_keywords": []},
        {"company": "Applied Intuition", "source": "ashby", "token": "applied", "title_keywords": []},
    ]))
    assert company_for_token("ashby", "retell-ai", wl) == "Retell"
    assert company_for_token("ashby", "APPLIED", wl) == "Applied Intuition"  # slug case is ignored
    assert company_for_token("lever", "retell-ai", wl) is None  # right slug, wrong board
    assert company_for_token("ashby", "unwatched", wl) is None
    assert company_for_token("ashby", "retell-ai", tmp_path / "missing.json") is None  # never raises

    assert titleize_token("retell-ai") == "Retell AI"
    assert titleize_token("composio") == "Composio"
    assert titleize_token("some_new_co") == "Some New Co"


def test_the_same_job_from_a_different_url_reuses_the_row_and_takes_the_ats_link(tmp_path):
    """The real tracker grew four duplicate pairs: a LinkedIn row from browsing,
    then a second row when the pipeline targeted the same job by its Ashby or
    Greenhouse URL. Dedup matched on the exact link, and those links differ - so
    the tracker said eight applications where there were four, and the row Duc
    had been curating was not the one the pipeline could autofill."""
    store = JobApplicationStore(tmp_path / "j.db")
    tool = TargetPostingTool(store, fetch=lambda url: fetch_posting(url, _fake))
    linkedin = "https://www.linkedin.com/jobs/view/4438446984/"
    first = tool.run(url=linkedin, posting_text="Join the Agent Platform team. Python.",
                     company="Netic", role="Software Engineer (Agent Platform) - New Grad")
    assert first["created"]

    ashby = "https://jobs.ashbyhq.com/netic/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f"
    again = tool.run(url=ashby)
    assert not again["created"], "same company and role should not open a second row"
    assert len(store.list()) == 1
    # And the row keeps the link the pipeline can actually act on.
    assert store.list()[0].link == ashby
    assert again["application"]["id"] == first["application"]["id"]


def test_a_different_role_at_the_same_company_is_still_its_own_row(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    tool = TargetPostingTool(store, fetch=lambda url: fetch_posting(url, _fake))
    tool.run(url="https://www.linkedin.com/jobs/view/1/", posting_text="text",
             company="Netic", role="Software Engineer (Agent Platform) - New Grad")
    tool.run(url="https://www.linkedin.com/jobs/view/2/", posting_text="text",
             company="Netic", role="Product Designer")
    assert len(store.list()) == 2


def test_greenhouse_embed_urls_are_recognized():
    """A company careers page usually hosts Greenhouse's embed form, and the
    'Apply on company website' link LinkedIn hands out lands there - so the
    URL Duc pastes is the embed one, not boards.greenhouse.io/<co>/jobs/<id>."""
    from companion.job_posting_fetch import parse_posting_url

    assert parse_posting_url("https://boards.greenhouse.io/embed/job_app?for=janestreet&token=1") == ("greenhouse", "janestreet", "1")
    assert parse_posting_url("https://job-boards.greenhouse.io/embed/job_app?token=1&for=janestreet&b=x") == ("greenhouse", "janestreet", "1")
    assert parse_posting_url("https://boards.greenhouse.io/embed/job_board?for=janestreet") is None


def test_source_url_ties_the_linkedin_row_to_the_posting_it_was_found_on(tmp_path):
    """Slice 3 of mass apply: Duc finds a job on LinkedIn, and pastes the
    'Apply on company website' link with it. The LinkedIn row he made while
    browsing may not match by title (LinkedIn's title and the ATS's differ), so
    the source URL is the exact key - and it is kept on the row, because 'where
    did I find this' is part of the record. LinkedIn itself is never read."""
    store = JobApplicationStore(tmp_path / "j.db")
    tool = TargetPostingTool(store, fetch=lambda url: fetch_posting(url, _fake))
    linkedin = "https://www.linkedin.com/jobs/view/4438446984/"
    first = tool.run(url=linkedin, posting_text="Trading systems in C++.", company="Meridian", role="Quant Dev")
    assert first["created"]

    gh = "https://job-boards.greenhouse.io/janestreet/jobs/1"
    again = tool.run(url=gh, source_url=linkedin)
    assert not again["created"] and again["application"]["id"] == first["application"]["id"]
    row = store.list()[0]
    assert row.link == gh, "the row keeps the URL an engine can fill"
    assert row.notes.count(f"[found via] {linkedin}") == 1
    tool.run(url=gh, source_url=linkedin)
    assert store.list()[0].notes.count("[found via]") == 1, "recorded once, not once per run"

    # A source URL on a job never seen before is simply recorded.
    fresh = tool.run(url="https://jobs.ashbyhq.com/netic/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f",
                     source_url="https://www.linkedin.com/jobs/view/1/")
    assert fresh["created"] and "[found via] https://www.linkedin.com/jobs/view/1/" in fresh["application"]["notes"]


def test_a_linkedin_url_with_no_text_says_how_to_hand_over_the_company_link(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    out = TargetPostingTool(store).run(url="https://www.linkedin.com/jobs/view/4438446984/")
    assert "error" in out and "never read" in out["error"] and "source_url" in out["error"]
    assert store.list() == []


WD_URL = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SWE_JR1"


def test_workday_urls_parse_in_all_the_shapes_they_are_pasted_in():
    """Workday is the most common ATS at large companies. Its URL carries the
    tenant in the subdomain and the site plus the posting path after the
    optional locale, and Duc pastes it having clicked Apply, so the wizard
    suffixes have to come off."""
    from companion.job_posting_fetch import parse_posting_url

    assert parse_posting_url(WD_URL) == ("workday", "nvidia", "NVIDIAExternalCareerSite/US-CA-Santa-Clara/SWE_JR1")
    # no locale segment, a different pod, and the apply wizard's own suffixes
    assert parse_posting_url("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/Dublin/Analyst_JR1") == (
        "workday", "salesforce", "External_Career_Site/Dublin/Analyst_JR1")
    assert parse_posting_url(WD_URL + "/apply") == parse_posting_url(WD_URL)
    assert parse_posting_url(WD_URL + "/apply/applyManually?source=x") == parse_posting_url(WD_URL)
    assert parse_posting_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite") is None


def test_workday_posting_is_fetched_from_its_public_json():
    """Same "official API, not scraping" line as the other three: this is the
    unauthenticated endpoint the careers site's own front end calls."""
    got = fetch_posting(WD_URL, _fake)
    assert got.source == "workday" and got.title == "Software Engineer, New Grad"
    assert "Build accelerated computing" in got.text and "- C++" in got.text
    assert got.posted_at == "2026-09-01" and got.location == "US, CA, Santa Clara"
    # NOT "2100 NVIDIA USA": hiringOrganization is the legal entity, and this string
    # becomes the tracker's company and the tailored resume's FILENAME.
    assert got.company == "Nvidia"


def test_a_workday_url_reaches_the_tracker_like_any_other_board(tmp_path):
    store = JobApplicationStore(tmp_path / "j.db")
    out = TargetPostingTool(store, fetch=lambda u: fetch_posting(u, _fake)).run(url=WD_URL)
    assert out["created"] and out["application"]["company"] == "Nvidia"
    assert out["application"]["status"] == "targeting" and out["posting_chars"] > 0
