"""Duc's own applicant profile - the data source for job-application
autofill (see job_autofill.py). One local JSON record, not a database
table, because there's exactly one of these: unlike job_applications.db
(many rows) or memory_notes (many dated entries), a profile is a single
current snapshot that gets edited in place.

Kept entirely local on purpose, same reasoning as everything else in
this project (see CLAUDE.md's voice/memory decisions) - this is the
most sensitive data Kyra touches (real name, contact info, resume), so
it stays in `data/applicant_profile.json` and nowhere else. Never
logged, never sent anywhere except directly into a form field on a
page Duc asked Kyra to fill.

EEO/voluntary self-identification fields (gender identity, race,
veteran/disability status) default to "Decline to self-identify" -
these are legally voluntary on every U.S. employer's Greenhouse form,
and guessing at them is never appropriate. Duc can override any
default by editing the JSON directly or via a future
`update_profile` tool - this module doesn't build that UI, just the
data model and the safe defaults.
"""
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from companion.paths import DATA_DIR

DEFAULT_PATH = DATA_DIR / "applicant_profile.json"

DECLINE = "Decline to self-identify"


@dataclass
class ApplicantProfile:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    country: str = "United States"

    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    twitter_url: str = ""
    current_company: str = ""

    preferred_name: str = ""
    pronouns: str = ""

    resume_path: str = ""  # absolute path to a local PDF/doc - required before autofill can attach one

    # EEO / voluntary self-identification - legally optional everywhere,
    # default to declining rather than guessing. Override individual
    # fields here if Duc wants real answers on file.
    eeo_gender_identity: str = DECLINE
    eeo_race_ethnicity: str = DECLINE
    eeo_hispanic_latino: str = DECLINE
    eeo_veteran_status: str = DECLINE
    eeo_disability_status: str = DECLINE

    def is_ready_for_autofill(self) -> list[str]:
        """Returns the list of missing required fields - empty means ready."""
        missing = []
        for field_name in ("first_name", "last_name", "email", "phone"):
            if not getattr(self, field_name):
                missing.append(field_name)
        if not self.resume_path:
            missing.append("resume_path")
        elif not Path(self.resume_path).is_file():
            missing.append(f"resume_path (file not found: {self.resume_path})")
        return missing


def load_profile(path: Path | str = DEFAULT_PATH) -> ApplicantProfile:
    path = Path(path)
    if not path.exists():
        return ApplicantProfile()
    data = json.loads(path.read_text(encoding="utf-8"))
    # Ignore unknown keys rather than erroring - lets the schema grow
    # without breaking on an older saved profile.
    known = {f: data[f] for f in ApplicantProfile.__dataclass_fields__ if f in data}
    return ApplicantProfile(**known)


def save_profile(profile: ApplicantProfile, path: Path | str = DEFAULT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(profile), indent=2), encoding="utf-8")
