"""Scheduled proposals feed the digest; previews read existing state only."""
import json
from pathlib import Path

from sqlalchemy import inspect

from companion.db import _engine_for, normalize_db_url
from companion.initiative_store import DB_PATH, DbInitiativeStore
from companion.initiatives import Evidence, GitSource, PatternsSource, ProjectNotesSource, RemindersSource, run_daily
from companion.paths import DATA_DIR
from companion.settings import get_settings

CACHE_DIR = DATA_DIR / 'initiatives'


def existing_store():
    url = get_settings().database_url
    if not url and not DB_PATH.exists():
        return None
    engine = _engine_for(normalize_db_url(url) if url else f'sqlite:///{DB_PATH}')
    if not inspect(engine).has_table('initiatives'):
        return None
    return DbInitiativeStore(engine=engine)


def populate_initiatives(data, *, write=True, llm=None, sources=None, cache_dir: Path = CACHE_DIR, store=None):
    """Read-only previews neither call a model nor create a cache or database."""
    day = data.generated_at.date()
    if not write:
        # Validate an existing cache too: corruption is not an empty suggestion list.
        run_daily(day, llm=None, sources=[], cache_dir=cache_dir, write=False)
        current = store if store is not None else existing_store()
        data.initiatives = [i for i in current.list() if i['status'] == 'proposed'] if current is not None else []
        return
    if llm is None:
        from anthropic import Anthropic

        from companion.llm import AnthropicLLM
        llm = AnthropicLLM(Anthropic(api_key=get_settings().require_api_key()), max_tokens=2500)
    if sources is None:
        sources = [RemindersSource(), ProjectNotesSource(), PatternsSource(), GitSource()]
    items = run_daily(day, llm=llm, sources=sources, cache_dir=cache_dir)
    raw = json.loads((cache_dir / f'{day.isoformat()}.json').read_text())
    current = store if store is not None else DbInitiativeStore()
    current.save(items, [Evidence(**e) for e in raw['evidence']], day)
    data.initiatives = [i for i in current.list() if i['status'] == 'proposed']
