"""Job application autofill - fills a Greenhouse application form from
Duc's local ApplicantProfile, using Playwright to drive a real,
visible browser window. Duc reviews and clicks Submit himself; this
never does it for him (same hard boundary as job_applications.py -
see that module's docstring and docs/agentic-roadmap.md job #4).

Strategy pattern like everything else here: `AutofillEngine` ABC so a
Lever/Workday/iCIMS engine can be added later without touching the
tool or the calling code, `GreenhouseAutofillEngine` is the only
concrete one today.

Field-matching approach, informed by inspecting a real live Greenhouse
form (a public Affirm posting, no data entered/submitted) rather than
guessing at structure:
- Core fields (name, email, phone, resume/cover-letter upload,
  LinkedIn) use consistent label text across every Greenhouse-hosted
  company - matched by label and filled directly.
- EEO/voluntary self-identification fields (gender identity, race,
  veteran/disability status) are also consistently labeled - filled
  from the profile's eeo_* fields, which default to declining rather
  than guessing (see profile.py).
- Custom questions are unique per company/posting (a referral-source
  dropdown, GitHub/portfolio links, "worked here before?", etc.) and
  can't be hardcoded. A small set of common ones get best-effort
  matched against profile fields (GitHub/Twitter/Portfolio link
  fields); everything else is left alone and reported as skipped
  rather than guessed at - never invent an answer to a question the
  profile has no data for.

Every run produces a FillReport and a saved Markdown summary
(data/job_autofill_logs/) - readable and diffable, listing exactly
what got filled and what didn't, so it's easy to check before hitting
submit. This is the "make sure it can be easily checked" requirement,
not an afterthought.
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from companion.paths import DATA_DIR
from companion.profile import ApplicantProfile, load_profile
from companion.tools import Tool

LOG_DIR = DATA_DIR / "job_autofill_logs"

# label substring (case-insensitive) -> ApplicantProfile attribute
CORE_FIELD_MAP = {
    "first name": "first_name",
    "last name": "last_name",
    "email": "email",
    "phone": "phone",
    "country": "country",
    "linkedin": "linkedin_url",
    "github": "github_url",
    "portfolio": "portfolio_url",
    "twitter": "twitter_url",
    "current company": "current_company",
    "preferred name": "preferred_name",
}

# Ashby renders ONE "Name" field where Greenhouse has first/last, and labels its
# link questions "Your GitHub" / "Your Personal Website". Checked before the core
# map so "name" alone resolves to the full name instead of falling through.
ASHBY_FIELD_MAP = {
    "personal website": "portfolio_url",
    "website": "portfolio_url",
}

EEO_FIELD_MAP = {
    "gender identity": "eeo_gender_identity",
    "hispanic": "eeo_hispanic_latino",
    "race": "eeo_race_ethnicity",
    "ethnicity": "eeo_race_ethnicity",
    "veteran status": "eeo_veteran_status",
    "disability status": "eeo_disability_status",
    "gender": "eeo_gender_identity",  # checked after the more specific keys above
}


@dataclass
class FilledField:
    label: str
    value: str


@dataclass
class SkippedField:
    label: str
    reason: str


@dataclass
class FillReport:
    url: str
    filled: list[FilledField] = field(default_factory=list)
    skipped: list[SkippedField] = field(default_factory=list)
    summary_path: str = ""

    def render_markdown(self) -> str:
        lines = [f"# Autofill report — {self.url}", "", f"Run at: {datetime.now().astimezone().isoformat()}", ""]
        lines.append(f"## Filled ({len(self.filled)})")
        lines += [f"- **{f.label}**: {f.value}" for f in self.filled] or ["(none)"]
        lines.append("")
        lines.append(f"## Skipped — needs your review ({len(self.skipped)})")
        lines += [f"- **{s.label}** — {s.reason}" for s in self.skipped] or ["(none)"]
        lines.append("")
        lines.append("Nothing was submitted. Review the open browser window against this list, then submit yourself.")
        return "\n".join(lines)


class AutofillEngine(ABC):
    """Interface: one concrete engine per ATS platform (Greenhouse today;
    Lever/Workday/iCIMS could each be a new subclass later without
    touching the tool that calls this).
    """

    @abstractmethod
    def fill(self, url: str, profile: ApplicantProfile) -> FillReport: ...


class LabeledFormEngine(AutofillEngine):
    """Shared behaviour for any ATS that renders standard `<label for=...>`
    markup: walk the labels, fill the fields whose label maps to a profile
    value, attach the resume, report everything else as skipped. Greenhouse
    and Ashby both do this; only the resume input and a couple of label
    conventions differ, which is what subclasses override.

    What this never does, on any ATS: touch a checkbox (consent and
    "willing to relocate" are Duc's to answer), answer a free-text custom
    question, or go anywhere near a CAPTCHA. And it never submits.
    """

    field_maps: tuple[dict, ...] = (CORE_FIELD_MAP, EEO_FIELD_MAP)
    resume_selector = "input[type=file]"
    # A selector that only exists once the form is really usable. Greenhouse
    # server-renders its form, so domcontentloaded is enough; Ashby renders it
    # client-side and a fill attempted at domcontentloaded finds an empty page
    # and reports every field missing (caught on a real Netic form, 2026-09-07).
    ready_selector: str | None = None
    ready_timeout_ms = 15000

    def __init__(self, headless: bool = False, log_dir: Path | str = LOG_DIR):
        # headless=False by default - Duc should see the browser fill
        # live, not just trust a log file. This is the point of
        # "fill-only, you submit": the window stays open for him.
        self._headless = headless
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def fill(self, url: str, profile: ApplicantProfile) -> FillReport:
        missing = profile.is_ready_for_autofill()
        if missing:
            raise ValueError(f"profile isn't ready for autofill, missing: {missing}")

        from playwright.sync_api import sync_playwright

        report = FillReport(url=url)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=self._headless)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        if self.ready_selector:
            try:
                page.wait_for_selector(self.ready_selector, timeout=self.ready_timeout_ms)
            except Exception:
                report.skipped.append(SkippedField(
                    label="(form)", reason=f"the form never rendered ({self.ready_selector}) - filled nothing"))
                report.summary_path = self._save_summary(report)
                return report

        # Every text/textarea input on the page, matched by its
        # associated <label> text - Greenhouse renders standard
        # <label for="..."> markup for both core and custom fields, so
        # one matching pass handles both; we just only *act* on labels
        # we recognize.
        for locator, label_text in self._labeled_text_inputs(page):
            self._fill_one(locator, label_text, profile, report)

        self._attach_resume(page, profile, report)

        report.summary_path = self._save_summary(report)
        # Deliberately don't close the browser or call playwright.stop()
        # here - the window needs to stay open for Duc to review and
        # submit. The process holding it open is the caller's script.
        return report

    def _labeled_text_inputs(self, page):
        """Yield (locator, label_text) for every text-like input that has
        an associated label - the general shape Greenhouse uses for
        every field, standard or custom.
        """
        labels = page.locator("label").all()
        for label in labels:
            text = (label.inner_text() or "").strip().rstrip("*").strip()
            if not text:
                continue
            input_id = label.get_attribute("for")
            if not input_id:
                continue
            # Greenhouse uses purely numeric ids (e.g. "4028768003") -
            # invalid as a bare CSS #id selector (identifiers can't start
            # with a digit), so match by attribute instead of id syntax.
            target = page.locator(f'[id="{input_id}"]')
            if target.count() == 0:
                continue
            tag = target.evaluate("el => el.tagName.toLowerCase()")
            input_type = (target.get_attribute("type") or "").lower()
            # "number" is here so a required numeric question ("years of industry
            # experience") is REPORTED as skipped rather than silently ignored -
            # the summary is what Duc checks before submitting, so a field missing
            # from it is worse than one listed as needing him.
            if tag == "textarea" or (tag == "input" and input_type in ("text", "tel", "email", "number", "url", "")):
                yield target, text

    def _fill_one(self, locator, label_text, profile: ApplicantProfile, report: FillReport) -> None:
        key = label_text.lower()
        attr = next((a for m in self.field_maps if (a := self._match(key, m))), None)
        if attr is None:
            report.skipped.append(SkippedField(label=label_text, reason="no matching profile field - custom question"))
            return
        value = getattr(profile, attr, "")
        if not value:
            report.skipped.append(SkippedField(label=label_text, reason=f"profile.{attr} is empty"))
            return
        try:
            locator.fill(value)
            report.filled.append(FilledField(label=label_text, value=value))
        except Exception as e:
            report.skipped.append(SkippedField(label=label_text, reason=f"couldn't fill: {e}"))

    @staticmethod
    def _match(label_key: str, field_map: dict) -> str | None:
        for substring, attr in field_map.items():
            if substring in label_key:
                return attr
        return None

    def _attach_resume(self, page, profile: ApplicantProfile, report: FillReport) -> None:
        resume_input = page.locator(self.resume_selector).first
        if resume_input.count() == 0 and self.resume_selector != "input[type=file]":
            resume_input = page.locator("input[type=file]").first
        if resume_input.count() == 0:
            report.skipped.append(SkippedField(label="Resume", reason="no file input found on page"))
            return
        try:
            resume_input.set_input_files(profile.resume_path)
            report.filled.append(FilledField(label="Resume", value=profile.resume_path))
        except Exception as e:
            report.skipped.append(SkippedField(label="Resume", reason=f"couldn't attach: {e}"))

    def _save_summary(self, report: FillReport) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", report.url.lower()).strip("-")[:60]
        ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        path = self._log_dir / f"{ts}-{slug}.md"
        path.write_text(report.render_markdown(), encoding="utf-8")
        return str(path)


_MIN_COMPANY_SLUG = 4


def _normalize_url(url: str) -> str:
    """Host + path, lowercased, no scheme/query/fragment/trailing slash - so a
    tracked link and the URL Duc pastes match even when one carries a
    `?gh_src=` tracking parameter."""
    u = urlparse(url.strip())
    host = (u.netloc or "").lower().removeprefix("www.")
    return f"{host}{(u.path or '').rstrip('/').lower()}"


def resume_for_url(url: str, applications: list) -> tuple[str | None, str]:
    """The company-tailored resume to attach for this posting, and why.

    Duc's rule (2026-09-07): one resume per company, tailored to that
    company's environment and specialisation. So the resume travels with the
    tracked application, and the profile default is only the fallback.

    Matching, most certain first: the same posting URL, then exactly one
    tracked company whose name appears in the URL (Greenhouse and Ashby both
    put the company slug in the path). An ambiguous match attaches nothing
    and says so - sending the wrong company's resume is worse than sending
    the general one.
    """
    target = _normalize_url(url)
    by_url = [a for a in applications if a.link and _normalize_url(a.link) == target]
    if len(by_url) == 1:
        app = by_url[0]
        if app.resume_path:
            return app.resume_path, f"tracked application #{app.id} ({app.company}) matched this URL"
        return None, f"tracked application #{app.id} ({app.company}) has no resume set - using the profile default"

    slug = re.sub(r"[^a-z0-9]+", "", target)
    by_company = []
    for a in applications:
        if not (a.resume_path and a.company):
            continue
        name = re.sub(r"[^a-z0-9]+", "", a.company.lower())
        # A very short name is a substring of half the internet ("AI" is inside
        # "openai"), so names under four characters must match by URL instead.
        if len(name) >= _MIN_COMPANY_SLUG and name in slug:
            by_company.append(a)
    if len(by_company) == 1:
        app = by_company[0]
        return app.resume_path, f"company {app.company!r} matched the URL (application #{app.id})"
    if len(by_company) > 1:
        names = ", ".join(a.company for a in by_company)
        return None, f"several tracked companies match this URL ({names}) - using the profile default"
    return None, "no tracked application matched this URL - using the profile default"


class GreenhouseAutofillEngine(LabeledFormEngine):
    """Greenhouse (boards.greenhouse.io / job-boards.greenhouse.io). Built
    against a real live posting. It needs no overrides: the base behaviour
    (labels, core/EEO maps, the first file input) is exactly Greenhouse's
    shape, which is why that behaviour lives in the base class.
    """


class AshbyAutofillEngine(LabeledFormEngine):
    """Ashby (jobs.ashbyhq.com). Built by reading two real live forms
    (Composio and Netic, 2026-09-07) rather than guessing the DOM, the same
    way the Greenhouse engine was built against a real Affirm posting.

    What the real forms showed: the core fields carry stable ids
    (`_systemfield_name`, `_systemfield_email`, `_systemfield_resume`),
    everything else is a custom question whose id is a UUID and whose label
    is human text, and a UUID beginning with a digit is not a valid bare CSS
    `#id` - the same attribute-selector lesson Greenhouse's numeric ids
    taught. Name is ONE field here, not first plus last.
    """

    field_maps = (ASHBY_FIELD_MAP, CORE_FIELD_MAP, EEO_FIELD_MAP)
    resume_selector = 'input[type=file][id="_systemfield_resume"]'
    ready_selector = 'input[id="_systemfield_email"]'

    @staticmethod
    def application_url(url: str) -> str:
        """Ashby serves the form at <posting>/application; the posting page
        itself only has an Apply button."""
        trimmed = url.split("?")[0].rstrip("/")
        return trimmed if trimmed.endswith("/application") else f"{trimmed}/application"

    def fill(self, url: str, profile: ApplicantProfile) -> FillReport:
        return super().fill(self.application_url(url), profile)

    def _fill_one(self, locator, label_text, profile: ApplicantProfile, report: FillReport) -> None:
        # The single "Name" field: first + last, and only when both exist.
        if label_text.strip().lower() in ("name", "full name"):
            full = f"{profile.first_name} {profile.last_name}".strip()
            if not full:
                report.skipped.append(SkippedField(label=label_text, reason="profile has no name"))
                return
            try:
                locator.fill(full)
                report.filled.append(FilledField(label=label_text, value=full))
            except Exception as e:  # noqa: BLE001 - one field failing must not stop the rest
                report.skipped.append(SkippedField(label=label_text, reason=f"couldn't fill: {e}"))
            return
        super()._fill_one(locator, label_text, profile, report)


ENGINES_BY_SOURCE: dict[str, type[LabeledFormEngine]] = {
    "greenhouse": GreenhouseAutofillEngine,
    "ashby": AshbyAutofillEngine,
}


def default_engines(headless: bool = False) -> dict[str, AutofillEngine]:
    """One engine per supported ATS, keyed the way job_posting_fetch parses a
    URL - so apply_pipeline.engine_for_url() finds them without a translation."""
    return {name: cls(headless=headless) for name, cls in ENGINES_BY_SOURCE.items()}


class AutofillJobApplicationTool(Tool):
    name = "autofill_job_application"
    description = (
        "Open a job application page (currently Greenhouse-hosted only) and fill in the standard fields "
        "(name, email, phone, resume, LinkedIn, etc.) from Duc's saved profile. Opens a real, visible browser "
        "window and leaves it open - it never clicks Submit. Also writes a plain-text summary of exactly what "
        "was filled and what was skipped (custom questions with no matching profile field), so it's easy to "
        "review before submitting. Fails clearly if the profile is missing required fields (name, email, "
        "phone, resume file)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The Greenhouse job application page URL"},
        },
        "required": ["url"],
    }

    def __init__(
        self, engine: AutofillEngine | None = None, profile: ApplicantProfile | None = None, applications=None,
    ):
        self._engine = engine or GreenhouseAutofillEngine()
        self._profile = profile
        self._applications = applications

    def _applications_list(self) -> list:
        if self._applications is not None:
            return self._applications.list()
        from companion.job_applications import JobApplicationStore

        return JobApplicationStore().list()

    def run(self, url: str) -> dict:
        profile = self._profile or load_profile()
        missing = profile.is_ready_for_autofill()
        if missing:
            return {
                "error": "profile isn't ready for autofill",
                "missing_fields": missing,
                "fix": "edit data/applicant_profile.json (or ask Duc to fill it in) before trying again",
            }

        # One resume per company: prefer the tracked application's own file.
        try:
            tailored, why = resume_for_url(url, self._applications_list())
        except Exception as e:  # noqa: BLE001 - a tracker read must never block an application
            tailored, why = None, f"couldn't read the tracker ({type(e).__name__}) - using the profile default"
        if tailored and not Path(tailored).is_file():
            why = f"{why}, but that file is missing ({tailored}) - using the profile default"
            tailored = None
        if tailored:
            profile = replace(profile, resume_path=tailored)

        report = self._engine.fill(url, profile)
        return {
            "url": url,
            "filled_count": len(report.filled),
            "skipped_count": len(report.skipped),
            "skipped_labels": [s.label for s in report.skipped],
            "resume_attached": Path(profile.resume_path).name,
            "resume_choice": why,
            "summary_path": report.summary_path,
            "note": "browser window left open for review - nothing was submitted",
        }


def job_autofill_tools(engine: AutofillEngine | None = None, applications=None) -> list[Tool]:
    return [AutofillJobApplicationTool(engine=engine, applications=applications)]
