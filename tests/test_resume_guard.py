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
