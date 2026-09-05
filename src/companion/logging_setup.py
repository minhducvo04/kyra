"""Standard-library logging, configured once per process.

Modules do `logger = logging.getLogger(__name__)` and log at the
appropriate level; only entry points (scripts/*.py, webapp startup)
call configure_logging(). Level comes from the environment
(`KYRA_LOG_LEVEL`, default INFO) so a debugging session can turn on
DEBUG without a code change, and a quiet run can set WARNING.

Why not print(): print output can't be filtered by level, has no
timestamps or source module, and can't be redirected to a file/
aggregator later without touching every call site. The one-page-fit
loop in particular benefits - "attempt 2 compiled to 2 pages, 9 lines
over" is exactly what you want in a log line when a result looks off.
"""
import logging
import os

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str | None = None) -> None:
    level_name = (level or os.environ.get("KYRA_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO), format=DEFAULT_FORMAT)
    # Third-party chatter that drowns out our own lines at INFO.
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "chromadb", "sentence_transformers", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
