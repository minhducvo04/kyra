from datetime import date

import pytest

from companion import lessons_log as ll

DAY = date(2026, 10, 12)


def put(root, agent="claude-lane-0", n=1, rule="Never write the retired name", day=DAY, package="RQ75"):
    for i in range(n):
        ll.add(agent, package, f"a comment broke a guard {i} {package} {day}", rule, "tests/test_word_scan.py", on=day, root=root)


def test_add_appends_one_line_and_never_rewrites(tmp_path):
    path = ll.add("Claude lane 0", "RQ75", "a comment named the retired word", "Keep it out of comments", "rename test", on=DAY, root=tmp_path)
    assert path == tmp_path / "claude-lane-0.md"
    first = path.read_text()
    ll.add("claude-lane-0", "RQ76", "second", "rule two", "check two", on=DAY, root=tmp_path)
    text = path.read_text()
    assert text.startswith(first) and text.count("\n") == 2
    assert text.splitlines()[0] == "- 2026-10-12 | RQ75 | a comment named the retired word | Keep it out of comments | rename test"


def test_recent_returns_the_last_five_oldest_first(tmp_path):
    for i in range(7):
        ll.add("codex-lane-2", f"RQ{i}", f"m{i}", f"r{i}", f"c{i}", on=DAY, root=tmp_path)
    rows = ll.recent("codex-lane-2", root=tmp_path)
    assert [r["package"] for r in rows] == ["RQ2", "RQ3", "RQ4", "RQ5", "RQ6"]
    assert ll.recent("nobody", root=tmp_path) == [] and ll.brief_section("nobody", root=tmp_path) == ""


def test_private_text_and_bad_fields_are_refused_and_nothing_is_written(tmp_path):
    bad = [("mistake", "see /Users/x/private"), ("rule", "mail me at someone@example.com"), ("check", "a|b"),
           ("mistake", "x" * 300), ("rule", "   "), ("mistake", "the blood pressure was high")]
    for field, value in bad:
        args = {"mistake": "m", "rule": "r", "check": "c", field: value}
        with pytest.raises(ValueError):
            ll.add("claude-lane-0", "RQ75", args["mistake"], args["rule"], args["check"], on=DAY, root=tmp_path)
    with pytest.raises(ValueError):
        ll.add("claude-lane-0", "../x", "m", "r", "c", root=tmp_path)
    assert list(tmp_path.glob("*")) == []
    safe = ll.add("../../etc/passwd/", "RQ1", "m", "r", "c", on=DAY, root=tmp_path)  # a name can never leave the folder
    assert safe == tmp_path / "etc-passwd.md"
    with pytest.raises(ValueError):
        ll.add("///", "RQ1", "m", "r", "c", root=tmp_path)


def test_weekly_counts_repeats_in_seven_days_and_flags_two_or_more(tmp_path):
    put(tmp_path, n=2, rule="Never write the retired name", day=DAY)
    put(tmp_path, n=1, rule="Inspect the screenshot", day=DAY, package="RQ77")
    put(tmp_path, n=3, rule="Old rule", day=date(2026, 10, 1))  # outside the window
    put(tmp_path, agent="codex-lane-1", n=1, rule="Never write the retired name", day=date(2026, 10, 7))
    rows = ll.weekly(today=DAY, root=tmp_path)
    by = {(r["agent"], r["rule"]): r for r in rows}
    assert by[("claude-lane-0", "Never write the retired name")]["count"] == 2
    assert by[("claude-lane-0", "Never write the retired name")]["repeat"] is True
    assert by[("claude-lane-0", "Inspect the screenshot")]["repeat"] is False
    assert by[("codex-lane-1", "Never write the retired name")]["count"] == 1  # 7 Oct is inside 6 to 12 Oct
    assert ("claude-lane-0", "Old rule") not in by
    table = ll.weekly_table(rows)
    assert "| claude-lane-0 | Never write the retired name | 2 | RQ75 | hand to RQ86 |" in table
    assert ll.weekly_table([]).startswith("No lessons")


def test_normalized_mistake_duplicate_preserves_existing_lesson(tmp_path):
    first = ll.add("Fixture Lane", "RQ1", "Missed the fixture.", "Inspect it", "Fixture check", root=tmp_path)
    before = first.read_text()
    assert ll.add("fixture-lane", "RQ2", " MISSED  the fixture?! ", "Another rule",
                  "Another check", root=tmp_path) == first
    assert first.read_text().startswith(before)  # the lesson line is kept as it was
    assert [r["rule"] for r in ll.read("fixture-lane", root=tmp_path)] == ["Inspect it"]
    ll.add("other-lane", "RQ2", "missed the fixture", "Inspect it", "Fixture check", root=tmp_path)
    assert len(ll.read("other-lane", root=tmp_path)) == 1
    ll.add("fixture-lane", "RQ2", "Missed a different fixture", "Inspect it", "Fixture check", root=tmp_path)
    assert len(ll.read("fixture-lane", root=tmp_path)) == 2


def test_a_repeated_mistake_keeps_one_lesson_but_counts_every_occurrence(tmp_path):
    ll.add("claude-lane-0", "RQ75", "Missed the guard.", "Run the guard", "guard test", on=date(2026, 10, 8), root=tmp_path)
    ll.add("claude-lane-0", "RQ80", "missed the guard", "Some other rule", "other check", on=date(2026, 10, 10), root=tmp_path)
    ll.add("claude-lane-0", "RQ81", "MISSED the guard!", "Run the guard", "guard test", on=DAY, root=tmp_path)
    ll.add("claude-lane-0", "RQ81", "missed the guard", "Run the guard", "guard test", on=DAY, root=tmp_path)  # a replay
    lessons = ll.read("claude-lane-0", root=tmp_path)
    assert len(lessons) == 1 and lessons[0]["package"] == "RQ75"
    assert ll.brief_section("claude-lane-0", root=tmp_path).count("\n- ") == 1
    seen = ll.occurrences("claude-lane-0", root=tmp_path)
    assert [(o["day"], o["package"]) for o in seen] == [(date(2026, 10, 10), "RQ80"), (DAY, "RQ81")]
    rows = ll.weekly(today=DAY, root=tmp_path)
    assert rows == [{"agent": "claude-lane-0", "rule": "Run the guard", "count": 3,
                     "packages": ["RQ75", "RQ80", "RQ81"], "repeat": True}]
    assert len(ll.weekly(today=date(2026, 10, 16), root=tmp_path)) == 1  # 8 Oct leaves the window, 10 and 12 Oct stay
    assert ll.weekly(today=date(2026, 10, 16), root=tmp_path)[0]["count"] == 2


def test_occurrence_lines_are_invisible_to_the_old_row_reader(tmp_path):
    path = tmp_path / "claude-lane-0.md"
    path.write_text("- 2026-10-12 | RQ75 | Missed the guard | Run the guard | guard test\n")
    ll.add("claude-lane-0", "RQ80", "missed the guard", "Run the guard", "guard test", on=DAY, root=tmp_path)
    lines = path.read_text().splitlines()
    assert lines[0] == "- 2026-10-12 | RQ75 | Missed the guard | Run the guard | guard test"
    assert len(lines) == 2 and not lines[1].startswith("- ")
    assert ll.weekly(today=DAY, root=tmp_path)[0]["count"] == 2
