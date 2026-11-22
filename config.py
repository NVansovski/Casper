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
    default_city: str = "Frankfurt am Main"
    pptx_search_dir: str = ""

    # Microsoft Graph (reserved for the future migration away from the mocks)
    ms_client_id: str = ""
    ms_tenant_id: str = ""

    # Local runtime
    timezone: str = "Europe/Berlin"
    data_file: Path = PROJECT_ROOT / "data.json"
    max_upload_bytes: int = 20 * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
