"""Ashby autofill engine. Built against two real live forms (Composio and
Netic, 2026-09-07); these tests pin the parts that were wrong at some point
during that build, without needing a browser."""
import pytest

from companion.apply_pipeline import engine_for_url
from companion.job_autofill import (
    AshbyAutofillEngine,
    FillReport,
    GreenhouseAutofillEngine,
    LabeledFormEngine,
    default_engines,
)
from companion.profile import ApplicantProfile


class FakeLocator:
    """Enough of a Playwright locator for the label walk and the fills."""

    def __init__(self, tag="input", input_type="text", label="", value_sink=None, present=True):
        self.tag, self.input_type, self.label = tag, input_type, label
        self.filled = None
        self.files = None
        self._present = present
        self._sink = value_sink

    def count(self):
        return 1 if self._present else 0

    def inner_text(self):
        return self.label

    def get_attribute(self, name):
        return {"for": self.label, "type": self.input_type}.get(name)

    def evaluate(self, _js):
        return self.tag

    def fill(self, value):
        self.filled = value
        if self._sink is not None:
            self._sink.append((self.label, value))

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
    walked = [label for _loc, label in AshbyAutofillEngine()._labeled_text_inputs(page)]
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
    # an ATS with no engine yet must resolve to None, not blow up
    assert engine_for_url("https://jobs.lever.co/acme/d9bcb6a2-0e54-4cb3-baec-43f2d74db18f", engines)[0] is None


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
