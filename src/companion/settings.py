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
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    tenant: str = Field(default="personal", validation_alias="KYRA_TENANT")
    owner_machine: bool = Field(default=False, validation_alias="KYRA_OWNER_MACHINE")
    vesync_username: str = Field(default="", validation_alias="VESYNC_USERNAME")
    vesync_password: SecretStr = Field(default="", validation_alias="VESYNC_PASSWORD")
    preapproved_tools: Annotated[frozenset[str], NoDecode] = Field(
        default=frozenset({
            "add_reminder", "complete_reminder", "snooze_reminder", "save_learning_item",
            "mark_learning_reviewed", "add_job_application", "update_job_application_status",
            "set_application_resume", "draft_application_material", "save_memory_note",
            "add_outreach_contact", "update_outreach_status", "draft_outreach_note",
            "target_job_posting", "start_focus_block", "end_focus_block",
        }),
        validation_alias="KYRA_PREAPPROVED_TOOLS",
    )
    outbound_gate: Literal["off", "dry_run", "enforce"] = Field(default="dry_run", validation_alias="KYRA_OUTBOUND_GATE")
    release_grants: Annotated[frozenset[str], NoDecode] = Field(
        default=frozenset({"conversation", "job_search"}), validation_alias="KYRA_RELEASE_GRANTS",
    )
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

    # Personal loop: paths are server configuration, never browser input.
    claude_cli_path: str = Field(default="", validation_alias="KYRA_CLAUDE_CLI_PATH")
    codex_cli_path: str = Field(default="", validation_alias="KYRA_CODEX_CLI_PATH")
    loop_timeout_seconds: float = Field(default=600, gt=0, le=1800, validation_alias="KYRA_LOOP_TIMEOUT_SECONDS")

    @field_validator("preapproved_tools", mode="before")
    @classmethod
    def validate_preapproved_tools(cls, value):
        if isinstance(value, str):
            value = frozenset(name.strip() for name in value.split(",") if name.strip())
        for name in sorted(set(value) & {"humidifier_control", "autofill_job_application", "copy_outreach_note"}):
            raise ValueError(f"{name} cannot be preapproved")
        return value

    @field_validator("release_grants", mode="before")
    @classmethod
    def validate_release_grants(cls, value):
        if isinstance(value, str):
            value = frozenset(name.strip() for name in value.split(",") if name.strip())
        forbidden = set(value) - {"conversation", "job_search"}
        if forbidden:
            raise ValueError(f"Cannot grant release for: {', '.join(sorted(forbidden))}")
        return value

    @model_validator(mode="after")
    def validate_tenant(self) -> "Settings":
        if self.tenant not in {"personal", "busy"}:
            raise ValueError(f"Unknown tenant: {self.tenant}")
        if self.tenant == "busy":
            # A nested worktree must also protect the main checkout's data.
            roots = [PROJECT_ROOT, *(p for p in PROJECT_ROOT.parents if (p / ".git").is_dir())]
            if any(self.data_dir.resolve().is_relative_to((root / "data").resolve()) for root in roots):
                raise ValueError("tenant busy requires KYRA_DATA_DIR outside the personal data directory")
        return self

    def require_api_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
