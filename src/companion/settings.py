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
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class MemorySettings(BaseModel):
    embedding_model: Literal['gemma2', 'bge-small'] = 'gemma2'

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, env_file_encoding='utf-8', extra='ignore')
    memory: MemorySettings = Field(default_factory=MemorySettings, validation_alias='KYRA_MEMORY')
    anthropic_api_key: str | None = Field(default=None, validation_alias='ANTHROPIC_API_KEY')
    tenant: str = Field(default='personal', validation_alias='KYRA_TENANT')
    owner_machine: bool = Field(default=False, validation_alias='KYRA_OWNER_MACHINE')
    preapproved_tools: Annotated[frozenset[str], NoDecode] = Field(default=frozenset(), validation_alias='KYRA_PREAPPROVED_TOOLS')
    outbound_gate: Literal['off', 'dry_run', 'enforce'] = Field(default='dry_run', validation_alias='KYRA_OUTBOUND_GATE')
    release_grants: Annotated[frozenset[str], NoDecode] = Field(default=frozenset(), validation_alias='KYRA_RELEASE_GRANTS')
    data_dir: Path = Field(default=PROJECT_ROOT / 'data', validation_alias='KYRA_DATA_DIR')
    obsidian_vault: Path | None = Field(default=None, validation_alias='KYRA_OBSIDIAN_VAULT')
    models_dir: Path | None = Field(default=None, validation_alias='KYRA_MODELS_DIR')
    log_level: str = Field(default='INFO', validation_alias='KYRA_LOG_LEVEL')
    classifier_adapter: str = Field(default='', validation_alias='KYRA_CLASSIFIER_ADAPTER')
    classifier_backend_adapter: str = Field(default='', validation_alias='KYRA_CLASSIFIER_BACKEND_ADAPTER')
    router_unsure_to_claude: float = Field(default=0, ge=0, le=1, validation_alias='KYRA_ROUTER_UNSURE_TO_CLAUDE')
    shadow_label_model_dir: Path = Field(default=Path.home() / 'kyra-model-trials/gemma-4-31b-it-4bit', validation_alias='KYRA_SHADOW_LABEL_MODEL_DIR')
    stt_model: str = Field(default='small', validation_alias='KYRA_STT_MODEL')
    voice_model: str = Field(default='', validation_alias='KYRA_VOICE_MODEL')
    remote_gpu_enabled: bool = Field(default=False, validation_alias='KYRA_REMOTE_GPU_ENABLED')
    remote_gpu_url: str = Field(default='', validation_alias='KYRA_REMOTE_GPU_URL')
    remote_gpu_model: Literal['gemma3:12b-it-q4_K_M', 'gemma3:27b-it-q4_K_M', 'devstral:24b-small-2505-q4_K_M'] = Field(default='gemma3:12b-it-q4_K_M', validation_alias='KYRA_REMOTE_GPU_MODEL')
    remote_gpu_timeout: float = Field(default=120, gt=0, le=600, allow_inf_nan=False, validation_alias='KYRA_REMOTE_GPU_TIMEOUT')
    ptt_key: str | None = Field(default=None, validation_alias='KYRA_PTT_KEY')
    interrupt_key: str | None = Field(default=None, validation_alias='KYRA_INTERRUPT_KEY')
    database_url: str = Field(default='', validation_alias='DATABASE_URL')
    inline_worker: bool = Field(default=True, validation_alias='KYRA_INLINE_WORKER')
    team_code_root: Path | None = Field(default=None, validation_alias='KYRA_TEAM_CODE_ROOT')
    team_primary: bool = Field(default=False, validation_alias='KYRA_TEAM_PRIMARY')
    team_apps_config: Path | None = Field(default=None, validation_alias='KYRA_TEAM_APPS_CONFIG')
    team_lead_config: Path | None = Field(default=None, validation_alias='KYRA_TEAM_LEAD_CONFIG')
    warm_up_router: bool = Field(default=True, validation_alias='KYRA_WARM_UP_ROUTER')
    api_token: str = Field(default='', validation_alias='KYRA_API_TOKEN')
    trust_loopback: bool = Field(default=True, validation_alias='KYRA_TRUST_LOOPBACK')
    session_ttl_days: int = Field(default=30, validation_alias='KYRA_SESSION_TTL_DAYS')
    access_team_domain: str = Field(default='', validation_alias='KYRA_ACCESS_TEAM_DOMAIN')
    access_aud: str = Field(default='', validation_alias='KYRA_ACCESS_AUD')
    access_local_hosts: str = Field(default='', validation_alias='KYRA_ACCESS_LOCAL_HOSTS')
    host: str = Field(default='127.0.0.1', validation_alias='KYRA_HOST')
    port: int = Field(default=8420, validation_alias='KYRA_PORT')
    tls_cert: str = Field(default='', validation_alias='KYRA_TLS_CERT')
    tls_key: str = Field(default='', validation_alias='KYRA_TLS_KEY')
    tls_terminated_upstream: bool = Field(default=False, validation_alias='KYRA_TLS_TERMINATED_UPSTREAM')
    claude_cli_path: str = Field(default='', validation_alias='KYRA_CLAUDE_CLI_PATH')
    codex_home: Path = Field(default_factory=lambda: Path.home() / '.codex', validation_alias='KYRA_CODEX_HOME')
    local_screen: bool = Field(default=True, validation_alias='KYRA_LOCAL_SCREEN')
    local_screen_model: str = Field(default='mlx-community/Qwen3.5-9B-4bit', validation_alias='KYRA_LOCAL_SCREEN_MODEL')
    team_honesty_auto: bool = Field(default=False, validation_alias='KYRA_TEAM_HONESTY_AUTO')
    codex_cli_path: str = Field(default='', validation_alias='KYRA_CODEX_CLI_PATH')
    loop_timeout_seconds: float = Field(default=600, gt=0, le=1800, validation_alias='KYRA_LOOP_TIMEOUT_SECONDS')
    loop_headless: bool = Field(default=False, validation_alias='KYRA_LOOP_HEADLESS')
    codex_task_id: str = Field(default='', validation_alias='KYRA_CODEX_TASK_ID')
    claude_session_name: str = Field(default='', validation_alias='KYRA_CLAUDE_SESSION_NAME')

    @field_validator('preapproved_tools', mode='before')
    @classmethod
    def validate_preapproved_tools(cls, value):
        if isinstance(value, str):
            value = frozenset((name.strip() for name in value.split(',') if name.strip()))
        for name in sorted(set(value) & {'bulb_control', 'humidifier_control', 'purifier_control'}):
            raise ValueError(f'{name} cannot be preapproved')
        return value

    @field_validator('release_grants', mode='before')
    @classmethod
    def validate_release_grants(cls, value):
        if isinstance(value, str):
            value = frozenset((name.strip() for name in value.split(',') if name.strip()))
        forbidden = set(value) - {'conversation', 'job_search'}
        if forbidden:
            raise ValueError(f"Cannot grant release for: {', '.join(sorted(forbidden))}")
        return value

    @model_validator(mode='after')
    def validate_tenant(self) -> 'Settings':
        if self.obsidian_vault is None:
            self.obsidian_vault = self.data_dir.resolve() / 'private_docs' / 'obsidian' / 'Kyra'
        self.obsidian_vault = self.obsidian_vault.expanduser()
        if self.models_dir is None:
            moved = Path.home() / 'kyra-models'
            self.models_dir = moved if moved.is_dir() else self.data_dir.resolve()
        self.models_dir = self.models_dir.expanduser()
        return self

    def require_api_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError('ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.')
        return self.anthropic_api_key

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
