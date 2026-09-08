"""Resolving a posting Duc found somewhere unfetchable (LinkedIn, a careers
page) to the company's own public board posting - mass-apply slice 3.

The property that matters most here is the refusal: guessing WRONG sends a
resume tailored for one job to a different one, which is worse than not
resolving at all. So an ambiguous or weak match must come back as None with a
reason, exactly like job_autofill.resume_for_url declines an ambiguous company.

Nothing in these tests reads LinkedIn; the only fetches are the three public
board APIs the watch already polls.
"""
import pytest

from companion.job_boards import (
    AshbyBoard,
    GreenhouseBoard,
    LeverBoard,
    WatchEntry,
    board_candidates,
    find_posting,
    save_watchlist,
    title_score,
)

ASHBY = {"jobs": [
    {"id": "aa11", "title": "Software Engineer, New Grad", "location": "SF",
     "jobUrl": "https://jobs.ashbyhq.com/pylon/aa11", "publishedAt": "2026-08-01T00:00:00+00:00"},
    {"id": "bb22", "title": "Account Executive", "location": "NYC",
     "jobUrl": "https://jobs.ashbyhq.com/pylon/bb22", "publishedAt": "2026-08-01T00:00:00+00:00"},
]}
GREENHOUSE = {"jobs": [
    {"id": 1, "title": "Software Engineer, New Grad", "location": {"name": "SF"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "first_published": "2026-08-01"},
    {"id": 2, "title": "Software Engineer, New Grad", "location": {"name": "NYC"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "first_published": "2026-08-01"},
]}
EMPTY: dict = {"jobs": []}


def _sources(ashby=ASHBY, greenhouse=EMPTY, lever=None):
    """Board sources over canned payloads. A source that raises stands for a
    board slug that does not exist, which is the normal case when a token is
    being guessed rather than read from the watchlist."""
    def _fail(_url):
        raise OSError("404")

    return {
        "greenhouse": GreenhouseBoard(lambda url: greenhouse),
        "lever": LeverBoard(lambda url: lever if lever is not None else _fail(url)),
        "ashby": AshbyBoard(lambda url: ashby),
    }


class TestTitleScore:
    def test_an_exact_title_scores_one(self):
        assert title_score("Software Engineer, New Grad", "Software Engineer, New Grad") == 1.0

    def test_punctuation_and_case_do_not_matter(self):
        assert title_score("software engineer - new grad", "Software Engineer, New Grad") == 1.0

    def test_a_board_title_with_extra_qualifiers_still_scores_high(self):
        # what boards actually do: the role plus a year, a level, an office
        assert title_score("Software Engineer, New Grad", "Software Engineer, New Grad (2026) - SF") > 0.7

    def test_a_different_role_scores_low(self):
        assert title_score("Software Engineer, New Grad", "Account Executive") < 0.3

    def test_a_related_but_wrong_role_does_not_reach_the_bar(self):
        """The dangerous near-miss: same words, different job. Must not pass."""
        assert title_score("Software Engineer, New Grad", "Engineering Manager") < 0.5


class TestBoardCandidates:
    def test_the_watchlist_is_preferred_over_a_guess(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Pylon", "ashby", "pylon", [])], path)
        candidates = board_candidates("Pylon", watchlist_path=path)
        assert candidates[0] == ("ashby", "pylon")

    def test_a_company_not_watched_is_guessed_across_every_board(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([], path)
        candidates = board_candidates("Nuvo", watchlist_path=path)
        assert set(candidates) == {("greenhouse", "nuvo"), ("lever", "nuvo"), ("ashby", "nuvo")}

    def test_a_multi_word_company_guesses_both_joined_and_hyphenated(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([], path)
        tokens = {token for _, token in board_candidates("Applied Intuition", watchlist_path=path)}
        assert "appliedintuition" in tokens and "applied-intuition" in tokens

    def test_a_missing_watchlist_still_guesses(self, tmp_path):
        assert board_candidates("Nuvo", watchlist_path=tmp_path / "nope.json")


class TestFindPosting:
    def test_a_watched_company_resolves_to_the_matching_posting(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Pylon", "ashby", "pylon", [])], path)
        match = find_posting("Pylon", "Software Engineer, New Grad", sources=_sources(), watchlist_path=path)
        assert match is not None
        assert match.posting.url == "https://jobs.ashbyhq.com/pylon/aa11"
        assert match.posting.source == "ashby" and match.score == 1.0
        assert "pylon" in match.reason

    def test_no_posting_close_enough_returns_nothing(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Pylon", "ashby", "pylon", [])], path)
        assert find_posting("Pylon", "Staff Data Scientist", sources=_sources(), watchlist_path=path) is None

    def test_two_equally_good_postings_are_refused_not_guessed(self, tmp_path):
        """Two offices, one title. Picking either could send Duc to the wrong
        req, so the resolver declines and the pipeline asks him."""
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Acme", "greenhouse", "acme", [])], path)
        sources = _sources(ashby=EMPTY, greenhouse=GREENHOUSE)
        assert find_posting("Acme", "Software Engineer, New Grad", sources=sources, watchlist_path=path) is None

    def test_a_board_that_does_not_exist_is_not_an_error(self, tmp_path):
        """Guessing tokens means most fetches 404. That is the expected path,
        not a failure - the resolver just keeps going."""
        path = tmp_path / "watchlist.json"
        save_watchlist([], path)
        match = find_posting("Pylon", "Software Engineer, New Grad", sources=_sources(), watchlist_path=path)
        assert match is not None and match.posting.source == "ashby"

    def test_every_board_failing_returns_nothing(self, tmp_path):
        def _fail(_url):
            raise OSError("404")

        sources = {
            "greenhouse": GreenhouseBoard(_fail), "lever": LeverBoard(_fail), "ashby": AshbyBoard(_fail),
        }
        assert find_posting("Ghost", "Software Engineer", sources=sources, watchlist_path=tmp_path / "w.json") is None

    def test_the_company_name_comes_from_the_watchlist_not_the_slug(self, tmp_path):
        """A board slug reaches the resume filename an employer sees, so the
        curated name wins - the same rule company_for_token already sets."""
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Retell", "ashby", "retell-ai", [])], path)
        match = find_posting("Retell", "Software Engineer, New Grad", sources=_sources(), watchlist_path=path)
        assert match is not None and match.posting.company == "Retell"

    @pytest.mark.parametrize("role", ["", "   "])
    def test_an_empty_role_is_refused(self, role, tmp_path):
        with pytest.raises(ValueError, match="role"):
            find_posting("Pylon", role, sources=_sources(), watchlist_path=tmp_path / "w.json")

    def test_an_empty_company_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="company"):
            find_posting("  ", "Software Engineer", sources=_sources(), watchlist_path=tmp_path / "w.json")


# --- The pipeline hand-off ---------------------------------------------------
# What this is for: Duc's tracker really does hold rows whose only link is a
# LinkedIn listing (Pylon, Nuvo), and they could not enter the apply pipeline at
# all. Resolving company + role to the company's own board posting is the way in
# that does not read LinkedIn.

from companion.apply_pipeline import ApplyError, resolve_apply_url, run_apply_pipeline  # noqa: E402
from companion.job_applications import JobApplicationStore  # noqa: E402
from companion.job_boards import Posting, PostingMatch  # noqa: E402
from companion.profile import ApplicantProfile  # noqa: E402
from tests.fakes import ScriptedLLM  # noqa: E402
from tests.latex_docs import make_doc, requires_latex  # noqa: E402
from tests.test_apply_pipeline import RecordingEngine  # noqa: E402
from tests.test_job_posting_fetch import _fake  # noqa: E402

LINKEDIN = "https://www.linkedin.com/jobs/view/4438850428/"
RESOLVED = "https://job-boards.greenhouse.io/janestreet/jobs/1"
ONE_PAGE = make_doc(20)
TAILORED = ONE_PAGE.replace("Bullet number 0:", "Led item 0 for the target:")


def _match(url=RESOLVED):
    return PostingMatch(
        posting=Posting("greenhouse", "Meridian", "1", "Software Engineer, New Grad", "NY", url, "2026-08-19"),
        score=1.0, reason=f"matched {url}",
    )


class TestResolveApplyUrl:
    def test_a_board_url_is_passed_straight_through(self, tmp_path):
        store = JobApplicationStore(tmp_path / "j.db")

        def _never(*a, **k):
            raise AssertionError("a board URL must not need resolving")

        assert resolve_apply_url(RESOLVED, store=store, resolve=_never) == (RESOLVED, "")

    def test_pasted_text_means_no_lookup(self, tmp_path):
        """Duc pasted the posting, so there is nothing to find."""
        store = JobApplicationStore(tmp_path / "j.db")

        def _never(*a, **k):
            raise AssertionError("pasted text must not trigger a board lookup")

        url, why = resolve_apply_url(LINKEDIN, store=store, posting_text="We are hiring", resolve=_never)
        assert url == LINKEDIN and why == ""

    def test_company_and_role_come_from_the_tracked_row(self, tmp_path):
        store = JobApplicationStore(tmp_path / "j.db")
        store.add("Pylon", "Software Engineer, New Grad", link=LINKEDIN, status="targeting")
        seen = {}

        def _resolve(company, role, **k):
            seen.update(company=company, role=role)
            return _match()

        url, why = resolve_apply_url(LINKEDIN, store=store, resolve=_resolve)
        assert url == RESOLVED and "matched" in why
        assert seen == {"company": "Pylon", "role": "Software Engineer, New Grad"}

    def test_explicit_company_and_role_win_over_the_row(self, tmp_path):
        store = JobApplicationStore(tmp_path / "j.db")
        store.add("Pylon", "Old Title", link=LINKEDIN, status="targeting")
        seen = {}

        def _resolve(company, role, **k):
            seen.update(company=company, role=role)
            return _match()

        resolve_apply_url(LINKEDIN, store=store, company="Nuvo", role="AI Engineer", resolve=_resolve)
        assert seen == {"company": "Nuvo", "role": "AI Engineer"}

    def test_no_company_or_role_anywhere_says_what_is_missing(self, tmp_path):
        store = JobApplicationStore(tmp_path / "j.db")
        with pytest.raises(ApplyError, match="company and role"):
            resolve_apply_url(LINKEDIN, store=store, resolve=lambda *a, **k: _match())

    def test_an_unresolvable_posting_says_what_to_do(self, tmp_path):
        store = JobApplicationStore(tmp_path / "j.db")
        store.add("Nuvo", "New Grad AI Engineer", link=LINKEDIN, status="targeting")
        with pytest.raises(ApplyError, match="Apply on company"):
            resolve_apply_url(LINKEDIN, store=store, resolve=lambda *a, **k: None)


@requires_latex
def test_a_linkedin_row_runs_through_the_pipeline_on_its_resolved_url(tmp_path):
    """End to end: the pasted URL is LinkedIn, everything downstream uses the
    board posting, and the tracker row keeps its identity while its link is
    upgraded to the one autofill can actually fill."""
    store = JobApplicationStore(tmp_path / "j.db")
    tracked = store.add("Meridian", "Software Engineer, New Grad", link=LINKEDIN, status="targeting")
    engine = RecordingEngine()
    result = run_apply_pipeline(
        LINKEDIN, store=store,
        resume_llm=ScriptedLLM([TAILORED, "BULLET: b | ASK: how many?"]), draft_llm=ScriptedLLM([]),
        base_latex=ONE_PAGE, profile=ApplicantProfile(first_name="Duc", last_name="Vo", email="d@x.com", phone="1"),
        engines={"greenhouse": engine}, fetch=lambda u: _fake_fetch(u), resolve=lambda *a, **k: _match(),
        resumes_dir=tmp_path / "resumes", cover_letters_dir=tmp_path / "letters",
    )
    assert result.status == "ready_to_submit" and result.attention == []
    assert result.url == RESOLVED and result.source_url == LINKEDIN
    assert engine.calls and engine.calls[0][0] == RESOLVED  # filled the board form, not LinkedIn
    row = store.get(tracked.id) if hasattr(store, "get") else store.list()[0]
    assert row.id == tracked.id and row.link == RESOLVED  # same row, better link
    assert any("linkedin.com" in line for line in result.steps)


def _fake_fetch(url):
    from companion.job_posting_fetch import fetch_posting

    return fetch_posting(url, _fake)


class TestGenericTitlesAreRefused:
    """Found against Anthropic's real board: a short role title matches many
    reqs, and the score alone picks whichever posting has the fewest extra
    words - confidently, and wrongly. A title that does not identify one
    posting must resolve to nothing."""

    BOARD = {"jobs": [
        {"id": 1, "title": "Applied AI Engineer", "location": {"name": "SF"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "first_published": "2026-08-01"},
        {"id": 2, "title": "AI Engineer, GTM Claudification", "location": {"name": "SF"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "first_published": "2026-08-01"},
    ]}

    def _sources(self):
        def _fail(_url):
            raise OSError("404")

        return {
            "greenhouse": GreenhouseBoard(lambda url: self.BOARD),
            "lever": LeverBoard(_fail), "ashby": AshbyBoard(_fail),
        }

    def test_a_generic_role_matching_several_reqs_is_refused(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Acme", "greenhouse", "acme", [])], path)
        assert find_posting("Acme", "AI Engineer", sources=self._sources(), watchlist_path=path) is None

    def test_the_full_title_still_resolves(self, tmp_path):
        path = tmp_path / "watchlist.json"
        save_watchlist([WatchEntry("Acme", "greenhouse", "acme", [])], path)
        match = find_posting("Acme", "AI Engineer, GTM Claudification", sources=self._sources(), watchlist_path=path)
        assert match is not None and match.posting.id == "2"
