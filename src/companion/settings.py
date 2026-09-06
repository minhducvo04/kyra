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
    ptt_key: str | None = Field(default=None, validation_alias="KYRA_PTT_KEY")
    interrupt_key: str | None = Field(default=None, validation_alias="KYRA_INTERRUPT_KEY")
    # Slice 2 switches the SQLite stores to this when it points at Postgres.
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    host: str = Field(default="127.0.0.1", validation_alias="KYRA_HOST")
    port: int = Field(default=8420, validation_alias="KYRA_PORT")

    def require_api_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
