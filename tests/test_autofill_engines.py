"""Ashby autofill engine. Built against two real live forms (Composio and
Netic, 2026-09-07); these tests pin the parts that were wrong at some point
during that build, without needing a browser."""
import sys
import types

import pytest

from companion.apply_pipeline import engine_for_url
from companion.job_autofill import (
    AshbyAutofillEngine,
    FillReport,
    GreenhouseAutofillEngine,
    LabeledFormEngine,
    LeverAutofillEngine,
    default_engines,
)
from companion.profile import ApplicantProfile


class FakeLocator:
    """Enough of a Playwright locator for the label walk and the fills."""

    def __init__(self, tag="input", input_type="text", label="", value_sink=None, present=True, count_override=None,
                 required=False, aria_required=None):
        self.tag, self.input_type, self.label = tag, input_type, label
        self.required, self.aria_required = required, aria_required
        self.filled = None
        self.files = None
        self._present = present
        self._sink = value_sink
        self._count_override = count_override

    def count(self):
        if self._count_override is not None:
            return self._count_override
        return 1 if self._present else 0

    def inner_text(self):
        return self.label

    def get_attribute(self, name):
        return {"for": self.label, "type": self.input_type, "aria-required": self.aria_required}.get(name)

    def evaluate(self, js):
        # the engine asks for the tag name, and separately for required-ness
        if "required" in js:
            return bool(self.required)
        return self.tag

    def fill(self, value):
        self.filled = value
        if self._sink is not None:
            self._sink[self.label] = value

    def set_input_files(self, path):
        self.files = path

    @property
    def first(self):
        return self


def _profile(**kw):
    base = dict(first_name="Duc", last_name="Vo", email="d@x.com", phone="555", resume_path="/r.pdf")
    base.update(kw)
    return ApplicantProfile(**base)


def test_application_url_is_normalized_and_idempotent():
    u = AshbyAutofillEngine.application_url
    assert u("https://jobs.ashbyhq.com/netic/abc") == "https://jobs.ashbyhq.com/netic/abc/application"
    assert u("https://jobs.ashbyhq.com/netic/abc/") == "https://jobs.ashbyhq.com/netic/abc/application"
    assert u("https://jobs.ashbyhq.com/netic/abc?utm=x") == "https://jobs.ashbyhq.com/netic/abc/application"
    already = "https://jobs.ashbyhq.com/netic/abc/application"
    assert u(already) == already


def test_ashby_fills_the_single_name_field():
    # Ashby has one "Name" input where Greenhouse has first and last; the core
    # map has no "name" key, so without the override this field was skipped.
    engine, report = AshbyAutofillEngine(), FillReport(url="u")
    loc = FakeLocator(label="Name")
    engine._fill_one(loc, "Name", _profile(), report)
    assert loc.filled == "Duc Vo"
    assert [f.label for f in report.filled] == ["Name"] and not report.skipped

    report2 = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Full Name"), "Full Name", _profile(first_name="", last_name=""), report2)
    assert report2.skipped[0].reason == "profile has no name"


def test_ashby_label_wording_maps_to_the_right_profile_fields():
    engine, report = AshbyAutofillEngine(), FillReport(url="u")
    p = _profile(github_url="https://gh/x", linkedin_url="https://li/x", portfolio_url="https://site/x")
    for label in ("Your GitHub", "Your LinkedIn", "Your Personal Website", "Phone Number"):
        engine._fill_one(FakeLocator(label=label), label, p, report)
    assert [f.value for f in report.filled] == ["https://gh/x", "https://li/x", "https://site/x", "555"]
    assert not report.skipped


def test_a_genuinely_custom_question_is_reported_never_guessed():
    engine, report = AshbyAutofillEngine(), FillReport(url="u")
    q = "What's the most interesting paper you've read in the past month?"
    engine._fill_one(FakeLocator(tag="textarea", label=q), q, _profile(), report)
    assert not report.filled and "custom question" in report.skipped[0].reason


class FakePage:
    def __init__(self, fields):
        self._labels = [FakeLocator(tag=t, input_type=ty, label=lab) for lab, t, ty in fields]
        self._by_label = {loc.label: loc for loc in self._labels}

    def locator(self, selector):
        if selector == "label":
            return self
        for label, loc in self._by_label.items():
            if f'"{label}"' in selector:
                return loc
        return FakeLocator(present=False)

    def all(self):
        return self._labels


def test_number_and_url_inputs_are_walked_so_they_reach_the_report():
    # A required numeric question was silently ignored on the real Netic form
    # because the type filter only allowed text/tel/email/textarea.
    page = FakePage([
        ("Name", "input", "text"),
        ("Years of experience", "input", "number"),
        ("Portfolio", "input", "url"),
        ("Additional Information", "textarea", "textarea"),
        ("Consent", "input", "checkbox"),
    ])
    walked = [label for _loc, label in AshbyAutofillEngine()._form_fields(page)]
    assert walked == ["Name", "Years of experience", "Portfolio", "Additional Information"]
    assert "Consent" not in walked  # a checkbox is Duc's to answer, never auto-ticked


def test_engines_are_keyed_the_way_posting_urls_parse():
    engines = default_engines()
    for url, expected in (
        ("https://jobs.ashbyhq.com/netic/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f", AshbyAutofillEngine),
        ("https://boards.greenhouse.io/acme/jobs/1", GreenhouseAutofillEngine),
    ):
        engine, ats = engine_for_url(url, engines)
        assert isinstance(engine, expected), (url, ats)
    assert isinstance(engine_for_url("https://jobs.lever.co/acme/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f", engines)[0],
                      LeverAutofillEngine)
    # An ATS with no engine must resolve to None rather than blow up. Workday is
    # recognized (it is fetched and tailored for) but will never have an engine:
    # its apply flow starts at Sign In, and Kyra never signs in or makes an
    # account - so the pipeline names it and explains, rather than saying "yet".
    from companion.apply_pipeline import CANNOT_FILL

    engine, ats = engine_for_url("https://acme.wd1.myworkdayjobs.com/careers/job/US-CA/Role_JR1", engines)
    assert engine is None and ats == "workday" and ats in CANNOT_FILL
    # a genuinely unknown site is still unknown
    engine, ats = engine_for_url("https://careers.example.com/job/1", engines)
    assert engine is None and ats == "unknown" and ats not in CANNOT_FILL


def test_greenhouse_behaviour_is_unchanged_by_the_shared_base():
    assert issubclass(GreenhouseAutofillEngine, LabeledFormEngine)
    assert GreenhouseAutofillEngine.resume_selector == "input[type=file]"
    assert GreenhouseAutofillEngine.ready_selector is None  # server-rendered, no wait needed
    engine, report = GreenhouseAutofillEngine(), FillReport(url="u")
    engine._fill_one(FakeLocator(label="First Name"), "First Name", _profile(), report)
    assert report.filled[0].value == "Duc"
    # Greenhouse must NOT gain Ashby's single-name behaviour
    report2 = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Name"), "Name", _profile(), report2)
    assert not report2.filled and report2.skipped


@pytest.mark.parametrize("engine", [AshbyAutofillEngine(), GreenhouseAutofillEngine()])
def test_an_empty_profile_field_says_so_rather_than_filling_blank(engine):
    report = FillReport(url="u")
    engine._fill_one(FakeLocator(label="LinkedIn"), "LinkedIn", _profile(linkedin_url=""), report)
    assert not report.filled and "linkedin_url is empty" in report.skipped[0].reason


class FakeLeverPage:
    """Lever identifies core fields by `name`, so the fake is selector-keyed."""

    def __init__(self, present, cards=0):
        self._present = set(present)
        self._cards = cards
        self.filled = {}

    def locator(self, selector):
        if selector.startswith('[name^="cards['):
            return FakeLocator(present=self._cards > 0, label="cards", count_override=self._cards)
        # names contain brackets ("urls[LinkedIn]"), so cut at the closing quote
        name = selector.split('name="')[1].split('"')[0] if 'name="' in selector else selector
        return FakeLocator(present=name in self._present, label=name, value_sink=self.filled)


def test_lever_application_url():
    u = LeverAutofillEngine.application_url
    assert u("https://jobs.lever.co/palantir/abc") == "https://jobs.lever.co/palantir/abc/apply"
    assert u("https://jobs.lever.co/palantir/abc/apply") == "https://jobs.lever.co/palantir/abc/apply"
    assert u("https://jobs.lever.co/palantir/abc?src=x") == "https://jobs.lever.co/palantir/abc/apply"


def test_lever_walks_named_fields_and_labels_them_like_the_other_boards():
    page = FakeLeverPage({"name", "email", "phone", "location", "org", "urls[LinkedIn]", "urls[GitHub]", "urls[Portfolio]"})
    got = [label for _loc, label in LeverAutofillEngine()._form_fields(page)]
    assert got == ["Name", "Email", "Phone", "Location", "Current company", "LinkedIn", "GitHub", "Portfolio"]
    # a form without the optional link fields yields only what is there
    thin = FakeLeverPage({"name", "email"})
    assert [lab for _l, lab in LeverAutofillEngine()._form_fields(thin)] == ["Name", "Email"]


def test_lever_labels_map_onto_the_shared_profile_maps():
    engine, report = LeverAutofillEngine(), FillReport(url="u")
    p = _profile(current_company="Escaype", linkedin_url="https://li/x", portfolio_url="https://site/x")
    for label in ("Name", "Current company", "LinkedIn", "Portfolio"):
        engine._fill_one(FakeLocator(label=label), label, p, report)
    assert [f.value for f in report.filled] == ["Duc Vo", "Escaype", "https://li/x", "https://site/x"]


def test_lever_reports_its_custom_questions():
    engine, report = LeverAutofillEngine(), FillReport(url="u")
    engine._attach_resume(FakeLeverPage({"name"}, cards=60), _profile(), report)
    assert "60 custom question field(s)" in {s.label for s in report.skipped}
    quiet = FillReport(url="u")
    engine._attach_resume(FakeLeverPage({"name"}, cards=0), _profile(), quiet)
    assert [s.label for s in quiet.skipped] == ["Resume"]  # the fake has no file input


def test_location_is_filled_when_set_and_flagged_when_not():
    """Lever's location box is required; the profile field exists so Duc can fill
    it once instead of typing a city on every application - but it is never guessed."""
    engine = LeverAutofillEngine()
    filled = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Location"), "Location", _profile(location="Berkeley, CA"), filled)
    assert filled.filled[0].value == "Berkeley, CA"
    empty = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Location"), "Location", _profile(), empty)
    assert "location is empty" in empty.skipped[0].reason


def test_chat_tool_picks_the_engine_from_the_url(tmp_path):
    """Asking Kyra in chat to autofill an Ashby posting must not use Greenhouse's
    selectors - the tool defaulted to Greenhouse for every URL until 2026-09-07."""
    from companion.job_applications import JobApplicationStore
    from companion.job_autofill import AutofillJobApplicationTool

    store = JobApplicationStore(tmp_path / "apps.db")
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4")
    tool = AutofillJobApplicationTool(profile=_profile(resume_path=str(resume)), applications=store)
    assert sorted(tool._engines) == ["ashby", "greenhouse", "lever"]

    out = tool.run("https://acme.wd1.myworkdayjobs.com/careers/job/1")
    assert "no autofill engine" in out["error"] and out["supported"] == ["ashby", "greenhouse", "lever"]

    # an explicit engine still wins, so tests and callers can inject one
    injected = GreenhouseAutofillEngine()
    assert AutofillJobApplicationTool(engine=injected)._engine is injected


def test_missing_browser_says_what_to_do(monkeypatch, tmp_path):
    """In a container the playwright package is installed but chromium is not, and
    that used to surface as an opaque traceback. Chromium stays out of the image on
    purpose - autofill opens a window Duc reviews and submits himself."""
    import companion.job_autofill as ja

    stopped = []

    class _Chromium:
        def launch(self, **kw):
            raise RuntimeError("Executable doesn't exist at /root/.cache/ms-playwright/...")

    class _PW:
        chromium = _Chromium()

        def stop(self):
            stopped.append(True)

    # fill() imports sync_playwright inside the function, so the module is the only
    # patch point - a module-level attribute on job_autofill is never consulted.
    # Injected into sys.modules rather than monkeypatched by dotted path, because
    # that form imports the real playwright to resolve it and playwright is not in
    # requirements-web.txt: CI has no such module, and the test died there while
    # passing on the laptop. Same trick the voice tests use for companion.voice.
    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: type("S", (), {"start": lambda self: _PW()})()
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
    (tmp_path / "r.pdf").write_bytes(b"%PDF-1.4")
    profile = ApplicantProfile(
        first_name="A", last_name="B", email="a@b.co", phone="1", resume_path=str(tmp_path / "r.pdf")
    )
    engine = GreenhouseAutofillEngine(log_dir=tmp_path)
    with pytest.raises(ja.BrowserUnavailable) as exc:
        engine.fill("https://boards.greenhouse.io/x/jobs/1", profile)
    assert "playwright install chromium" in str(exc.value)
    assert stopped, "the playwright process was leaked instead of stopped"


def test_required_fields_are_recognised_the_way_each_board_marks_them():
    """Checked on the real live forms (2026-09-08), and no two agree:

    - **Ashby** sets the DOM `required` property and puts no marker in the label.
    - **Greenhouse** leaves `required` FALSE, and marks it with `aria-required="true"`
      plus an asterisk in the label text.

    So required-ness is the union of the three signals; reading only one would
    call every Greenhouse field optional or every Ashby one."""
    engine = GreenhouseAutofillEngine()
    assert engine._is_required(FakeLocator(required=True), "Name") is True                      # Ashby
    assert engine._is_required(FakeLocator(aria_required="true"), "First Name*") is True        # Greenhouse
    assert engine._is_required(FakeLocator(aria_required="false"), "LinkedIn Profile") is False
    assert engine._is_required(FakeLocator(), "Location") is False


def test_an_optional_empty_field_is_reported_but_is_not_a_reason_to_stop():
    """Duc has no portfolio site, so every form offering a Portfolio box used to
    turn a perfect application into needs_attention. A status that says "look at
    this" when there is nothing to look at is a status he stops trusting."""
    engine, report = GreenhouseAutofillEngine(), FillReport(url="u")
    engine._fill_one(FakeLocator(label="Portfolio"), "Portfolio", _profile(), report)
    skipped = report.skipped[0]
    assert skipped.reason == "profile.portfolio_url is empty" and skipped.required is False

    required_report = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Portfolio*", required=True), "Portfolio*", _profile(), required_report)
    assert required_report.skipped[0].required is True


def test_a_required_custom_question_is_the_one_that_must_be_flagged():
    """The Netic form's "How many years of industry experience" is required and
    has no profile field to answer it from. Treating every custom question as
    routine hid exactly the case that stops a submission."""
    engine = GreenhouseAutofillEngine()
    report = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Years of experience", required=True), "Years of experience", _profile(), report)
    assert report.skipped[0].required is True and "custom question" in report.skipped[0].reason

    optional = FillReport(url="u")
    engine._fill_one(FakeLocator(label="Anything else?"), "Anything else?", _profile(), optional)
    assert optional.skipped[0].required is False
