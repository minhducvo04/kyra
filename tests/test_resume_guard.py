from companion.resume_guard import check_resume_output, number_tokens, unsupported_numbers, unsupported_urls

ORIGINAL = r"""GPA: 3.42 \\ 2023 -- 2024 \\ improved latency by 40\% for 1,000 users
\vspace{-5pt} % a LaTeX comment mentioning 999
GPT-4 and H100 and 3B models. github.com/duc/kyra"""


def test_number_tokens_normalize_and_skip_comments_and_alnum():
    toks = number_tokens(ORIGINAL)
    assert {"3.42", "2023", "2024", "40", "1000"} <= toks
    assert "5" not in toks  # "-5pt" is a layout unit glued to letters, not a fact
    assert "999" not in toks  # inside a LaTeX comment
    assert "4" not in toks and "100" not in toks and "3" not in toks  # GPT-4 / H100 / 3B stay whole


def test_unchanged_output_is_clean():
    assert check_resume_output(ORIGINAL, [ORIGINAL]) == []


def test_new_numbers_and_links_are_flagged():
    output = ORIGINAL.replace("40\\%", "45\\%") + " served 12 teams. linkedin.com/in/duc"
    assert unsupported_numbers(output, [ORIGINAL]) == ["12", "45"]
    assert unsupported_urls(output, [ORIGINAL]) == ["linkedin.com/in/duc"]
    warnings = check_resume_output(output, [ORIGINAL])
    assert len(warnings) == 2
    assert "45" in warnings[0] and "12" in warnings[0]
    assert "linkedin.com/in/duc" in warnings[1]


def test_extra_facts_count_as_a_source():
    output = ORIGINAL + " graduated 2026"
    assert unsupported_numbers(output, [ORIGINAL]) == ["2026"]
    assert unsupported_numbers(output, [ORIGINAL, "Duc graduates in 2026"]) == []


ORIGINAL_TEX = r"""\resumeSubheading{University of California, Berkeley}{June 2024 -- August 2026}{B.S. EECS}{GPA: 3.42}
\resumeItem{\textbf{Coursework}: Machine Learning (CS 189), Data Structures (CS 61B)}
\resumeSubheading{Escaype LLC}{Novato, CA}{Software Engineer Intern}{Sep. 2024 -- Present}
\resumeItem{Reduced OpenRouter API overhead by 85\% using sidecar JSON metadata.}
% \resumeProjectHeading{\textbf{\href{https://github.com/x/f500}{\underline{Fortune 500 Analysis}}} $|$ \emph{pandas}}{June 2023}
% \resumeItem{Web-scraped data from 1955 to 2023 into SQLite.}
"""


def test_commented_stash_counts_as_source_evidence():
    out = ORIGINAL_TEX.replace("% \\resumeProjectHeading", "\\resumeProjectHeading").replace("% \\resumeItem{Web", "\\resumeItem{Web")
    assert check_resume_output(out, [ORIGINAL_TEX]) == []


def test_output_comments_are_ignored():
    out = ORIGINAL_TEX + "% \\resumeItem{Boosted revenue 300\\% at Fabricated Corp}\n"
    assert check_resume_output(out, [ORIGINAL_TEX]) == []


def test_fabricated_course_code_and_heading_are_caught():
    from companion.resume_guard import unsupported_courses, unsupported_headings, unsupported_terms

    out = ORIGINAL_TEX.replace("Data Structures (CS 61B)", "Data Structures (CS 61B), Operating Systems (CS 162)")
    assert unsupported_courses(out, [ORIGINAL_TEX]) == ["CS 162"]
    assert "Operating Systems" in unsupported_terms(out, [ORIGINAL_TEX])
    out2 = ORIGINAL_TEX + r"\resumeSubheading{Acme Robotics}{Remote}{ML Engineer}{2025}" + "\n"
    assert unsupported_headings(out2, [ORIGINAL_TEX]) == ["Acme Robotics"]
    warnings = check_resume_output(out2, [ORIGINAL_TEX])
    assert any("entry heading" in w and "Acme Robotics" in w for w in warnings)


def test_reworded_bullet_with_known_words_is_not_flagged():
    from companion.resume_guard import unsupported_terms

    out = ORIGINAL_TEX.replace("Reduced OpenRouter API overhead", "Cut OpenRouter API overhead")
    assert unsupported_terms(out, [ORIGINAL_TEX]) == []
