"""Speech-to-text. The abstract base allows swapping in a local engine later."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import openai
from openai import AsyncOpenAI

from pipeline.errors import TranscriptionError

logger = logging.getLogger(__name__)


class SpeechToText(ABC):
    """Converts an audio file into text. Implementations must raise :class:`TranscriptionError`."""

    @abstractmethod
    async def transcribe(self, audio: bytes, filename: str) -> str:
        """Return the transcript of ``audio``. ``filename`` carries the format extension."""


class WhisperAPISpeechToText(SpeechToText):
    """Transcription via the OpenAI Whisper API (``whisper-1`` by default)."""

    def __init__(self, client: AsyncOpenAI, *, model: str, language: str) -> None:
        self._client = client
        self._model = model
        self._language = language

    async def transcribe(self, audio: bytes, filename: str) -> str:
        content_type = "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav"
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                file=(filename, audio, content_type),
                language=self._language,
            )
        except openai.APITimeoutError as exc:
            raise TranscriptionError(
                "Die Spracherkennung hat zu lange gedauert. Bitte versuch es noch einmal."
            ) from exc
        except openai.APIConnectionError as exc:
            logger.error("Whisper API unreachable: %s", exc)
            raise TranscriptionError(
                "Ich kann die Spracherkennung gerade nicht erreichen. Prüf bitte die Internetverbindung."
            ) from exc
        except openai.APIStatusError as exc:
            logger.error("Whisper API returned HTTP %s: %s", exc.status_code, exc.message)
            raise TranscriptionError(
                "Die Spracherkennung hat leider einen Fehler gemeldet. Versuch es gleich noch einmal."
            ) from exc
        return result.text.strip()
