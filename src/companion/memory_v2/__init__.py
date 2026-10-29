"""Memory v2: typed, local SQLite memory, one database file per scope.

Phase 1 (docs/plans/2026-10-09-memory-v2.md): the store, a budgeted prompt
assembler, a Markdown export view and a dry-run importer. Phase 2 (wiring.py)
wires packs into the chat and Team prompts behind KYRA_MEMORY_V2=1, off by default.
"""
