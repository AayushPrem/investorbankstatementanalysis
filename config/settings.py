"""Environment-driven settings (pydantic-settings).

This is the only place the default LLM model id lives. Every LLM call goes
through ``llm/client.py``, which reads its model, timeout and retry policy
from here.

``get_settings()`` is deliberately *not* cached. Analysts decide whether the
LLM path is enabled when they are constructed, and tests toggle
``ANTHROPIC_API_KEY`` with ``monkeypatch``/``patch.dict`` right before that.
A cached settings object would freeze the first value it saw. Building one is
cheap (it only reads the environment).

A ``.env`` file is read from the path in ``BSAA_ENV_FILE`` (default
``.env``). Set ``BSAA_ENV_FILE=""`` to disable dotenv loading entirely; the
test suite does this so a developer's local ``.env`` can't switch tests into
LLM mode.
"""
from __future__ import annotations

import os

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_MODEL = "claude-haiku-4-5-20251001"


class Settings(BaseSettings):
    """Process-wide settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: SecretStr | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_enabled: bool = True
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2  # same as the anthropic SDK default
    database_url: str | None = None
    log_level: str = "INFO"
    log_json: bool = True
    models_dir: str = "models"
    embeddings_enabled: bool = False

    # ── Phase 2: data platform (db/, api/, worker/) ─────────────────────────
    # SHA-256 hex digest of the API key clients send in X-API-Key. Unset → the
    # API refuses every authenticated request (fail closed).
    api_key_hash: SecretStr | None = None
    max_upload_bytes: int = 20 * 1024 * 1024
    job_lease_s: float = 300.0       # a running job untouched this long is reclaimable
    job_max_attempts: int = 3
    job_backoff_s: float = 5.0       # retry delay = job_backoff_s * 2**(attempts-1)
    worker_poll_s: float = 1.0

    @property
    def llm_available(self) -> bool:
        """True when the LLM may be called: enabled and a non-empty key is set."""
        key = self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else ""
        return self.llm_enabled and bool(key)


def _env_file() -> str | None:
    path = os.environ.get("BSAA_ENV_FILE", ".env")
    return path or None


def get_settings() -> Settings:
    """Build a fresh Settings from the current environment (and .env, if any)."""
    return Settings(_env_file=_env_file())  # type: ignore[call-arg]


def llm_key_configured() -> bool:
    """True when ANTHROPIC_API_KEY is set (env or .env) and LLM_ENABLED is not false.

    This replaces the ``bool(os.getenv("ANTHROPIC_API_KEY"))`` checks that
    were repeated in each analyst.
    """
    return get_settings().llm_available
