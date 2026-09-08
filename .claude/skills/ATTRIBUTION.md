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

## License notices

MIT requires that the copyright notice and the permission notice travel with the copied files, so
naming the license in the table above is not on its own sufficient. The three upstream notices:

- `grilling`, `domain-modeling`, `grill-me`, `grill-with-docs` — Copyright (c) 2026 Matt Pocock
- `code-review-and-quality`, `test-driven-development`, `interview-me` — Copyright (c) 2025 Addy Osmani
- `ponytail`, `ponytail-review` — Copyright (c) 2026 DietrichGebert

All nine are distributed under the MIT License:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction,
> including without limitation the rights to use, copy, modify, merge, publish, distribute,
> sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
> NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
> DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
> OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

Copyright lines verified against each project's own `LICENSE` on 2026-09-08. This repo's own license
is a separate question and is still open — see `data/private_docs/needs-your-input.md`.
