# Initiatives review follow-through

- Preserve complete fenced JSON parsing; skip invalid rows individually -> verify: mixed valid/invalid rows keep only valid proposals and never log raw text.
- Move final-result behavior to Tool.terminal and Tool.render_result -> verify: a different named tool ends the loop, sibling calls are disclosed, source failures propagate, and normal turns still work.
- Keep conflicting evidence IDs as errors: silently choosing one could attach the wrong evidence -> verify: existing collision regression.
- Compare against the independent branch and run a real hosted proposal -> verify: exact evidence survives, full pytest and ruff pass.

No merge or push. The terminal boundary begins at the model response requesting that tool; it does not undo earlier actions.
