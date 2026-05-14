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
    DotEnvSettingsSource,
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
    # Resolved against `~` if it starts with `~`. None → $TEND_HOME/workspace/.
    workspace_dir: str | None = None


class SchedulerConfig(BaseModel):
    """Scheduler configuration for proactive triggers."""

    heartbeat_every: str = "30m"  # or "off"
    missed_at_policy: str = "run-on-restart"  # or "skip"


class WebhookConfig(BaseModel):
    """Webhook server configuration for receiving proactive events."""

    host: str = "127.0.0.1"
    # 47331 picked deliberately: 7331 collides with VS Code's helper port,
    # which is a near-universal "first-time tend user" headache on macOS.
    # Override via [webhook] port in tend.toml if this clashes for you.
    port: int = 47331


class AnnouncerConfig(BaseModel):
    """Announcer configuration for proactive notifications."""

    default_cooldown_s: int = 300
    category: dict[str, int] = {}


class GoogleEventConfig(BaseModel):
    """Per-tag config in `[google.events.<tag>]` blocks."""

    upcoming_lead: str = "0m"
    emit: list[str] = ["starting"]


class GoogleConfig(BaseModel):
    """Google integration config from `[google]` block."""

    watched_calendars: list[str] = []
    events: dict[str, GoogleEventConfig] = {}


class Settings(BaseSettings):
    """All tend settings. Field names are lowercase; env vars are TEND_<UPPERCASE>."""

    model_config = SettingsConfigDict(
        env_prefix="TEND_",
        extra="ignore",
        # env_file and toml_file resolved dynamically via paths module —
        # see settings_customise_sources below.
    )

    # LLM brain
    llm_model: str = "claude-haiku-4-5"

    # STT
    deepgram_model: str = "nova-3-general"
    whisper_model: str = "tiny.en"

    # TTS
    # TTS provider (see roadmap item #5; macOS port lands the provider field early)
    tts_provider: str = "auto"           # auto | elevenlabs | avspeech | piper
    avspeech_voice: str = ""             # AVSpeechSynthesisVoice identifier; empty = system default
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    elevenlabs_model: str = "eleven_turbo_v2_5"
    # Playback speed for ElevenLabs. None keeps the API default (1.0).
    # The API accepts roughly 0.7–1.2; values outside that range will
    # be rejected by ElevenLabs.
    elevenlabs_speed: float | None = None
    piper_voice: str = "en_US-ryan-high"

    # Audio
    sample_rate: int = 16000

    # Audio path & AEC
    mic_channels: int | None = None         # platform default: 1 on macOS, 2 on Linux
    aec_engine: str = "auto"                # auto | webrtc-aec3 | speex | off

    # Wake / sleep
    openwakeword_model: str = "hey_jarvis"
    wake_threshold: float = 0.5
    sleep_phrase: str = "goodbye"
    sleep_fuzz_ratio: float = 0.85
    awake_timeout_s: int = 30

    # Day-session
    daily_reset_time: str = "04:00"
    timezone: str | None = None  # None → system default

    # Per-worker config blocks. Keys are worker names; values are WorkerConfig.
    workers: dict[str, WorkerConfig] = {}

    # Proactive triggers
    scheduler: SchedulerConfig = SchedulerConfig()
    webhook: WebhookConfig = WebhookConfig()
    announcer: AnnouncerConfig = AnnouncerConfig()
    google: GoogleConfig = GoogleConfig()

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
    tend_webhook_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TEND_WEBHOOK_TOKEN", "tend_webhook_token"),
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
        # Re-build dotenv + toml sources with runtime-resolved paths so
        # $TEND_HOME overrides take effect (the model_config's env_file/
        # toml_file are evaluated at class definition time, which is too
        # early — `tend.paths` reads the env var at call time).
        from tend.paths import env_path, toml_path

        dotenv = DotEnvSettingsSource(settings_cls, env_file=str(env_path()))
        toml = TomlConfigSettingsSource(settings_cls, toml_file=str(toml_path()))
        return (init_settings, env_settings, dotenv, toml, file_secret_settings)


# Pull keyring-stored secrets into os.environ before Settings is built. Pydantic
# only reads from env vars + dotenv; without this step, secrets saved by
# `tend setup` to the OS keyring (the default on macOS) are invisible at boot
# and the Brain LLM preflight reports "ANTHROPIC_API_KEY not set."
from tend.secrets import load_into_env as _load_secrets_into_env

_load_secrets_into_env()

settings = Settings()
