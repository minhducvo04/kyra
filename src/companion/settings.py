"""One typed Settings object for the whole process (12-factor config).

Before this, nine call sites read os.environ directly with their own
defaults, and the API key lived in config.py's import-time global. A
single pydantic-settings model means: every knob is listed in one place
with its type and default, `.env` is loaded once, tests override by env
var exactly as before, and a container gets configured the same way as
the laptop. Existing variable names are unchanged so nothing in .env or
CI has to move.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    data_dir: Path = Field(default=PROJECT_ROOT / "data", validation_alias="KYRA_DATA_DIR")
    log_level: str = Field(default="INFO", validation_alias="KYRA_LOG_LEVEL")
    classifier_adapter: str = Field(default="", validation_alias="KYRA_CLASSIFIER_ADAPTER")  # "repo:adapter_dir"
    stt_model: str = Field(default="small", validation_alias="KYRA_STT_MODEL")
    # A second Claude model for SPOKEN text turns only (needs-your-input #24, measured in
    # docs/voice-latency.md: claude-haiku-4-5 reaches its first token ~3x sooner than
    # sonnet-5 on the same prompt). Unset, every turn keeps the default model. Never
    # applied to tool turns - Claude stays the Agent Specialist on the tool loop by
    # measurement (docs/tool-calling-distill.md) - or to turns the router sends local.
    voice_model: str = Field(default="", validation_alias="KYRA_VOICE_MODEL")
    ptt_key: str | None = Field(default=None, validation_alias="KYRA_PTT_KEY")
    interrupt_key: str | None = Field(default=None, validation_alias="KYRA_INTERRUPT_KEY")
    # Slice 2 switches the SQLite stores to this when it points at Postgres.
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    # v2 slice 3: run the job worker as a thread inside the web process (laptop
    # default). The container sets this False and runs scripts/worker.py.
    inline_worker: bool = Field(default=True, validation_alias="KYRA_INLINE_WORKER")
    # Load the router's classifier when the server starts rather than making the
    # first turn wait ~44s for it (measured 2026-09-08). Off in tests, where
    # starting the app must never load a model.
    warm_up_router: bool = Field(default=True, validation_alias="KYRA_WARM_UP_ROUTER")
    # Mass apply tailors every resume from this .tex (relative paths resolve under data_dir). The library copy of
    # the LaTeX source went stale once, so the base is a file Duc edits directly, not a library document.
    resume_base_tex: str = Field(default="resumes/Duc_Vo_Resume_General_AI_Engineer.tex", validation_alias="KYRA_RESUME_BASE_TEX")
    # LAN readiness (2026-09-07, for the Vision Pro client): when set, any request to /api/*
    # from a non-loopback address must carry "Authorization: Bearer <token>". Unset keeps the
    # laptop exactly as it was. KYRA_HOST=0.0.0.0 is the switch that exposes the server at all.
    api_token: str = Field(default="", validation_alias="KYRA_API_TOKEN")
    # The loopback exemption above reads the socket peer address and never
    # X-Forwarded-For, which is attacker-controlled. That is safe behind a proxy on
    # another host (the peer is then the proxy, so remote callers must authenticate)
    # but wrong when a reverse proxy runs on the SAME host as Kyra, where the peer is
    # 127.0.0.1 for everyone. Set this false in that shape and every caller logs in.
    trust_loopback: bool = Field(default=True, validation_alias="KYRA_TRUST_LOOPBACK")
    session_ttl_days: int = Field(default=30, validation_alias="KYRA_SESSION_TTL_DAYS")
    host: str = Field(default="127.0.0.1", validation_alias="KYRA_HOST")
    port: int = Field(default=8420, validation_alias="KYRA_PORT")

    def require_api_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
