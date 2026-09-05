# Third-party skills in .claude/skills/

Vendored as plain SKILL.md files (reviewed before adding; no hooks, no scripts) on 2026-09-05.
Pull updates by re-fetching the file from the source path. Each file keeps its upstream frontmatter.

| Skill | Source | License |
|---|---|---|
| `grilling` | https://github.com/mattpocock/skills | MIT |
| `domain-modeling` | https://github.com/mattpocock/skills | MIT |
| `grill-me` | https://github.com/mattpocock/skills | MIT |
| `grill-with-docs` | https://github.com/mattpocock/skills | MIT |
| `code-review-and-quality` | https://github.com/addyosmani/agent-skills | MIT |
| `test-driven-development` | https://github.com/addyosmani/agent-skills | MIT |
| `interview-me` | https://github.com/addyosmani/agent-skills | MIT |
| `ponytail` | https://github.com/DietrichGebert/ponytail | MIT |
| `ponytail-review` | https://github.com/DietrichGebert/ponytail | MIT |

Usage in this repo: `/grill-me` before a non-trivial build; `/interview-me` when an ask is ambiguous;
`/test-driven-development` and `/code-review-and-quality` during build/review; `/ponytail` on build sessions
to keep code minimal; `/ponytail-review` to hunt over-engineering in a diff; `/domain-modeling` keeps CONTEXT.md.
