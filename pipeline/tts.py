"""Text-to-speech. The abstract base allows swapping in a local engine later."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import openai
from openai import AsyncOpenAI

from pipeline.errors import SynthesisError

logger = logging.getLogger(__name__)

MEDIA_TYPES: dict[str, str] = {"wav": "audio/wav", "mp3": "audio/mpeg"}


class SpeechSynthesizer(ABC):
    """Turns text into audio bytes. Implementations must raise :class:`SynthesisError`."""

    @property
    @abstractmethod
    def media_type(self) -> str:
        """MIME type of the bytes returned by :meth:`synthesize`."""

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Return spoken audio for ``text``."""


class OpenAISpeechSynthesizer(SpeechSynthesizer):
    """Speech via the OpenAI TTS API (``tts-1`` by default)."""

    def __init__(self, client: AsyncOpenAI, *, model: str, voice: str, response_format: str) -> None:
        if response_format not in MEDIA_TYPES:
            raise ValueError(f"Unsupported TTS response format: {response_format!r}")
        self._client = client
        self._model = model
        self._voice = voice
        self._response_format = response_format

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self._response_format]

    async def synthesize(self, text: str) -> bytes:
        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,  # type: ignore[arg-type]  # validated by the API
                input=text,
                response_format=self._response_format,  # type: ignore[arg-type]
            )
        except openai.APITimeoutError as exc:
            raise SynthesisError("Meine Stimme hat gerade zu lange gebraucht. Bitte frag noch einmal.") from exc
        except openai.APIConnectionError as exc:
            logger.error("TTS API unreachable: %s", exc)
            raise SynthesisError(
                "Ich kann meine Stimme gerade nicht erreichen. Prüf bitte die Internetverbindung."
            ) from exc
        except openai.APIStatusError as exc:
            logger.error("TTS API returned HTTP %s: %s", exc.status_code, exc.message)
            raise SynthesisError("Meine Stimme hat gerade einen Fehler gemeldet.") from exc

        audio = response.content
        if not audio:
            raise SynthesisError("Ich konnte keine Antwort vorlesen.")
        return audio
