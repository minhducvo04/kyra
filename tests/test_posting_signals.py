"""Posting signals are deterministic: every flag is a regex or a count, so
each of Duc's rules (data/private_docs/job-search-notes-2026-09-06.md) gets
a case that must fire and a case that must stay quiet."""
from datetime import UTC, datetime

from companion.posting_signals import AnalyzePostingTool, analyze_posting, render_signals

NOW = datetime(2026, 9, 7, tzinfo=UTC)

NORTHWIND = """Software Engineer – University Graduate (US)
At Northwind, our Software Engineers work in small teams to turn the best ideas into high-performing and resilient
technology. You'll apply innovative techniques including distributed computing, natural language processing,
machine learning and more.

Your Skills & Talents:
* Bachelor's or master's in computer science, engineering or related fields
* Exceptional programming (C++, Python preferred) and design skills
* Strong analytical skills and familiarity with probability and statistics
* Ability to communicate effectively in a collaborative, complex and highly technical team environment

In accordance with applicable law, the base salary range for this role is $235,000 to $300,000.
"""

MESSY = """Software Engineer
Join the Payments Reliability team. We are working to improve the stability of our checkout platform after a
year of incidents; you will be on-call and will migrate the legacy Rails monolith. You will work with many
stakeholders across product, finance and support and navigate ambiguity in a fast-paced environment.
Requirements
- 1-2 years of experience
- Python
- Go
- Kubernetes
- Terraform
- Postgres
- Kafka
- React
- TypeScript
- AWS
- GraphQL
- Redis
- Datadog
- Airflow
Compensation: $90k - $200k. Candidates must be authorized to work in the United States; we do not offer visa
sponsorship. This role is on-site in Austin, TX.
"""


def test_northwind_posting_is_clean_except_pooled_hiring():
    sig = analyze_posting(NORTHWIND, title="Software Engineer – University Graduate (US)", posted_at="2026-09-06", now=NOW)
    assert sig.age_days == 1 and sig.priority == "normal"
    assert (sig.salary_min, sig.salary_max) == (235_000, 300_000) and sig.salary_ratio < 2
    assert sig.junior and sig.requirement_count == 4
    assert sig.problems == []  # "high-performing" alone is not a scale complaint
    assert any("degree" in s.lower() or "bachelor" in s.lower() for s in sig.mandatory)
    assert not sig.team_named
    assert [f for f in sig.flags if "pooled" in f]
    assert sig.questions == []


def test_messy_posting_fires_every_rule():
    sig = analyze_posting(MESSY, title="Software Engineer", posted_at="2026-07-01", reposted=True, now=NOW)
    assert sig.age_days == 68 and any("shortlist" in f for f in sig.flags)
    assert sig.reposted and any("REPOSTED" in f for f in sig.flags) and sig.priority == "high"  # repost outranks stale
    assert (sig.salary_min, sig.salary_max) == (90_000, 200_000) and any("spans 2.2x" in f for f in sig.flags)
    assert sig.junior and sig.requirement_count == 14 and any("14 requirement bullets" in f for f in sig.flags)
    assert {"stability", "process", "legacy"} <= set(sig.problems)
    assert len(sig.questions) == len(sig.problems)
    assert any("authorized to work" in s for s in sig.mandatory) and any("on-site" in s for s in sig.mandatory)
    assert sig.team_named and sig.team == "Payments Reliability"
    text = render_signals(sig)
    assert text.startswith("Priority: high") and "Mandatory conditions" in text and "? " in text


def test_hourly_and_monthly_figures_are_not_salary_ranges():
    sig = analyze_posting("Intern. $40/hr - $60/hr. Housing stipend $6,000 - $10,000.")
    assert sig.salary_min is None and not any("salary" in f for f in sig.flags)


def test_tool_shape_and_bad_date():
    tool = AnalyzePostingTool()
    out = tool.run(posting_text=NORTHWIND, title="Software Engineer", posted_at="2026-09-06")
    assert out["summary"].startswith("Priority") and out["salary_ratio"] == 1.28
    assert "error" in tool.run(posting_text="x", posted_at="yesterday")


def test_mandatory_regex_does_not_fire_on_optimize():
    sig = analyze_posting("You will optimize other agents autonomously and author evaluation harnesses. We accept OPT and CPT.")
    assert len(sig.mandatory) == 1 and "OPT and CPT" in sig.mandatory[0]
