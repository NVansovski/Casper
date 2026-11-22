"""Pipeline error types. Every message is German because it is spoken to the user."""

from __future__ import annotations


class CasperError(Exception):
    """Base error for expected pipeline failures."""

    def __init__(self, spoken_message: str) -> None:
        super().__init__(spoken_message)
        self.spoken_message = spoken_message


class ConfigurationError(CasperError):
    """A required API key or setting is missing."""


class TranscriptionError(CasperError):
    """Speech-to-text failed."""


class ReasoningError(CasperError):
    """The LLM call or the tool loop failed."""


class SynthesisError(CasperError):
    """Text-to-speech failed."""
