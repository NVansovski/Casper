"""Text-to-speech. The abstract base allows swapping engines; the default is edge-tts."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import aiohttp
import edge_tts
from edge_tts.exceptions import EdgeTTSException

from pipeline.errors import SynthesisError

logger = logging.getLogger(__name__)

SYNTHESIS_TIMEOUT_S = 30.0


class SpeechSynthesizer(ABC):
    """Turns text into audio bytes. Implementations must raise :class:`SynthesisError`."""

    @property
    @abstractmethod
    def media_type(self) -> str:
        """MIME type of the bytes returned by :meth:`synthesize`."""

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Return spoken audio for ``text``."""


class EdgeSpeechSynthesizer(SpeechSynthesizer):
    """Speech via ``edge-tts`` (Microsoft Edge neural voices, no API key, MP3 output)."""

    def __init__(self, *, voice: str) -> None:
        self._voice = voice

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def media_type(self) -> str:
        return "audio/mpeg"

    async def synthesize(self, text: str) -> bytes:
        try:
            async with asyncio.timeout(SYNTHESIS_TIMEOUT_S):
                communicate = edge_tts.Communicate(text, self._voice)
                chunks = [
                    chunk["data"]
                    async for chunk in communicate.stream()
                    if chunk["type"] == "audio"
                ]
        except TimeoutError as exc:
            raise SynthesisError(
                "Meine Stimme hat gerade zu lange gebraucht. Bitte frag noch einmal."
            ) from exc
        except (EdgeTTSException, aiohttp.ClientError, OSError) as exc:
            logger.error("edge-tts failed: %s", exc)
            raise SynthesisError(
                "Ich kann meine Stimme gerade nicht erreichen. Prüf bitte die Internetverbindung."
            ) from exc

        audio = b"".join(chunks)
        if not audio:
            raise SynthesisError("Ich konnte keine Antwort vorlesen.")
        return audio
