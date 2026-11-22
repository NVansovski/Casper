"""Application configuration, loaded from environment variables and `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Path = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Typed settings. Field names map case-insensitively to environment variables."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    assistant_name: str = "Casper"

    # Anthropic (reasoning and tool routing)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_max_tokens: int = 1024

    # Local speech-to-text (faster-whisper); the model downloads on first run
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "de"

    # Text-to-speech (edge-tts, MP3 output)
    tts_voice: str = "de-DE-ConradNeural"

    # Tool configuration
    default_city: str = "Ulm"
    pptx_search_dir: str = ""

    # Microsoft Graph. With MS_CLIENT_ID set, the real calendar, To Do and mail tools
    # replace the local mock tools. MS_TENANT_ID may be empty ("common") for most accounts.
    ms_client_id: str = ""
    ms_tenant_id: str = ""
    msal_cache_file: Path = PROJECT_ROOT / ".msal_cache.json"

    # Google (optional Gmail integration). Without credentials.json the Gmail tools stay off.
    google_credentials_file: Path = PROJECT_ROOT / "credentials.json"
    google_token_file: Path = PROJECT_ROOT / "token_google.json"

    # Web search, transit
    search_region: str = "de-de"
    transit_api_url: str = "https://v6.db.transport.rest"

    # Local runtime
    timezone: str = "Europe/Berlin"
    data_file: Path = PROJECT_ROOT / "data.json"
    max_upload_bytes: int = 20 * 1024 * 1024

    @property
    def graph_enabled(self) -> bool:
        """True when a Microsoft app registration is configured."""
        return bool(self.ms_client_id.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
