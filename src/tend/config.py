"""Settings for tend, loaded from class defaults, tend.toml, .env, and env vars.

Precedence (highest first):
1. Environment variables (TEND_<KEY> for project settings; vendor SDK conventions for secrets)
2. .env file (gitignored, secrets only)
3. tend.toml (committed defaults)
4. Class defaults (in this file)
"""

from __future__ import annotations

from typing import Type

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class WorkerConfig(BaseModel):
    """Per-worker overrides loaded from `[workers.<name>]` blocks in tend.toml."""

    model: str | None = None
    setting_sources: str = "user"
    allowed_tools: list[str] = []
    mcp_config_path: str | None = None
    # Persistent workspace where the worker builds and accumulates artifacts.
    # Resolved against `~` if it starts with `~`. None → ~/.tend/workspace/.
    workspace_dir: str | None = None
    # General worker: directory containing skill subdirs (each with a SKILL.md).
    # Resolved against `~` if it starts with `~`. None → ~/.tend/skills/.
    # NOTE: skills_dir affects the GeneralWorker only. The voice-side
    # `list_skills` tool and the `tend skills ...` CLI subcommands look at
    # TEND_SKILLS_ROOT (env var) or ~/.tend/skills. If you override
    # skills_dir, set TEND_SKILLS_ROOT to the same value to keep them aligned.
    skills_dir: str | None = None


class Settings(BaseSettings):
    """All tend settings. Field names are lowercase; env vars are TEND_<UPPERCASE>."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TEND_",
        toml_file="tend.toml",
        extra="ignore",
    )

    # LLM brain
    llm_model: str = "claude-haiku-4-5"

    # STT
    deepgram_model: str = "nova-3-general"
    whisper_model: str = "tiny.en"

    # TTS
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    elevenlabs_model: str = "eleven_turbo_v2_5"
    piper_voice: str = "en_US-ryan-high"

    # Audio
    sample_rate: int = 16000

    # Wake / sleep
    openwakeword_model: str = "hey_jarvis"
    wake_threshold: float = 0.5
    sleep_phrase: str = "goodbye jarvis"
    sleep_fuzz_ratio: float = 0.85
    awake_timeout_s: int = 30

    # Day-session
    daily_reset_time: str = "04:00"
    timezone: str | None = None  # None → system default
    soul_path: str = "soul.md"

    # Logging
    log_path: str = "/tmp/tend.log"

    # Per-worker config blocks. Keys are worker names; values are WorkerConfig.
    workers: dict[str, WorkerConfig] = {}

    # Secrets (vendor conventions, no TEND_ prefix)
    # AliasChoices lets tests pass deepgram_api_key=... directly in Settings(...) while
    # env-var loading still reads the canonical vendor names (ANTHROPIC_API_KEY, etc.)
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key"),
    )
    deepgram_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPGRAM_API_KEY", "deepgram_api_key"),
    )
    elevenlabs_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ELEVENLABS_API_KEY", "elevenlabs_api_key"),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


settings = Settings()
