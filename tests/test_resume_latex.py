from companion.resume_latex import content_diff, restore_comments

ORIG = """\\documentclass{article}
\\begin{document}
% template note
\\resumeSubheading{Escaype}{2024}{Intern}{Now}
\\resumeItem{Did A.}
% \\resumeItem{Alternate A bullet}
\\resumeItem{Did B.}
% \\resumeProjectHeading{Old project}{2023}
% \\resumeItem{old bullet}
\\end{document}
"""


def test_restore_comments_puts_dropped_stash_back_after_anchor():
    edited = "\n".join(line for line in ORIG.splitlines() if not line.lstrip().startswith("%")) + "\n"
    restored, n = restore_comments(ORIG, edited)
    assert n == 4
    lines = restored.splitlines()
    assert lines.index("% \\resumeItem{Alternate A bullet}") == lines.index("\\resumeItem{Did A.}") + 1
    assert lines.index("% template note") == lines.index("\\begin{document}") + 1
    assert lines.index("% \\resumeItem{old bullet}") < lines.index("\\end{document}")
    # idempotent
    again, n2 = restore_comments(ORIG, restored)
    assert n2 == 0 and again == restored


def test_restore_comments_when_anchor_was_cut_goes_before_end():
    edited = ORIG.replace("\\resumeItem{Did B.}\n", "").replace("% \\resumeProjectHeading{Old project}{2023}\n", "")
    edited = "\n".join(line for line in edited.splitlines() if "old bullet" not in line) + "\n"
    restored, n = restore_comments(ORIG, edited)
    assert n == 2
    lines = restored.splitlines()
    assert lines[-2] == "% \\resumeItem{old bullet}" and lines[-1] == "\\end{document}"


def test_content_diff_detects_unchanged_reworded_removed_reordered():
    assert content_diff(ORIG, ORIG).unchanged
    reworded = ORIG.replace("Did A.", "Shipped A for the target role.")
    d = content_diff(ORIG, reworded)
    assert (d.bullets_kept, d.bullets_reworded, d.bullets_removed) == (1, 1, 0) and not d.unchanged
    removed = ORIG.replace("\\resumeItem{Did B.}\n", "")
    assert content_diff(ORIG, removed).bullets_removed == 1
    reordered = ORIG.replace("% \\resumeItem{Alternate A bullet}\n", "").replace(
        "\\resumeItem{Did A.}\n\\resumeItem{Did B.}", "\\resumeItem{Did B.}\n\\resumeItem{Did A.}")
    d = content_diff(ORIG, reordered)
    assert d.order_changed and "order changed" in d.summary()
    added = ORIG.replace("\\end{document}", "\\resumeSubheading{Fabricated Corp}{}{}{}\n\\end{document}")
    assert content_diff(ORIG, added).headings_added == 1
