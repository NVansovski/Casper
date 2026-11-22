"""Wires STT, brain and TTS into one request-scoped voice pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from config import Settings
from pipeline.brain import CasperBrain
from pipeline.errors import CasperError, ConfigurationError, TranscriptionError
from pipeline.stt import SpeechToText, WhisperAPISpeechToText
from pipeline.tts import OpenAISpeechSynthesizer, SpeechSynthesizer
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

OPENAI_TIMEOUT_S = 30.0
ANTHROPIC_TIMEOUT_S = 45.0

GENERIC_ERROR = "Da ist leider etwas schiefgegangen. Versuch es bitte noch einmal."
EMPTY_TRANSCRIPT = "Ich habe leider nichts verstanden. Sprich bitte etwas deutlicher."
MISSING_OPENAI_KEY = (
    "Mir fehlt der OpenAI-Schlüssel für Sprache. Bitte trag ihn in der .env-Datei ein."
)
MISSING_ANTHROPIC_KEY = (
    "Mir fehlt der Anthropic-Schlüssel für mein Denken. Bitte trag ihn in der .env-Datei ein."
)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outcome of one utterance. ``status`` is ``"error"`` when a spoken error was returned."""

    status: Literal["ok", "error"]
    transcript: str
    response_text: str
    audio: bytes
    media_type: str


class VoicePipeline:
    """Speech in, speech out. Expected failures become spoken German replies."""

    def __init__(
        self,
        settings: Settings,
        *,
        stt: SpeechToText,
        brain: CasperBrain,
        tts: SpeechSynthesizer,
        openai_client: AsyncOpenAI,
        anthropic_client: AsyncAnthropic,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._brain = brain
        self._tts = tts
        self._openai_client = openai_client
        self._anthropic_client = anthropic_client

    @property
    def tool_names(self) -> list[str]:
        return self._brain.tool_names

    async def process(self, audio: bytes, filename: str) -> PipelineResult:
        """Transcribe, reason and synthesise. Only a TTS failure is raised to the caller."""
        transcript = ""
        status: Literal["ok", "error"] = "ok"
        try:
            self._ensure_configured()
            transcript = await self._stt.transcribe(audio, filename)
            if not transcript:
                raise TranscriptionError(EMPTY_TRANSCRIPT)
            logger.info("Transcript: %s", transcript)
            reply = await self._brain.respond(transcript)
        except CasperError as exc:
            logger.warning("Pipeline stopped: %s", exc.spoken_message)
            reply = exc.spoken_message
            status = "error"
        except Exception:
            logger.exception("Unexpected pipeline failure")
            reply = GENERIC_ERROR
            status = "error"

        logger.info("Reply: %s", reply)
        speech = await self._tts.synthesize(reply)
        return PipelineResult(
            status=status,
            transcript=transcript,
            response_text=reply,
            audio=speech,
            media_type=self._tts.media_type,
        )

    async def aclose(self) -> None:
        """Release HTTP connections held by the API clients."""
        await self._openai_client.close()
        await self._anthropic_client.close()

    def _ensure_configured(self) -> None:
        if not self._settings.openai_api_key:
            raise ConfigurationError(MISSING_OPENAI_KEY)
        if not self._settings.anthropic_api_key:
            raise ConfigurationError(MISSING_ANTHROPIC_KEY)


def build_pipeline(settings: Settings) -> VoicePipeline:
    """Construct the production pipeline. Missing keys are reported per request, not at startup."""
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=OPENAI_TIMEOUT_S)
    anthropic_client = AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=ANTHROPIC_TIMEOUT_S
    )
    registry = ToolRegistry.discover()
    timezone = ZoneInfo(settings.timezone)

    tts_format = settings.tts_response_format.lower()
    if tts_format not in ("wav", "mp3"):
        raise ValueError("TTS_RESPONSE_FORMAT must be 'wav' or 'mp3'.")

    return VoicePipeline(
        settings,
        stt=WhisperAPISpeechToText(
            openai_client, model=settings.stt_model, language=settings.stt_language
        ),
        brain=CasperBrain(
            anthropic_client,
            registry,
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            assistant_name=settings.assistant_name,
            timezone=timezone,
        ),
        tts=OpenAISpeechSynthesizer(
            openai_client,
            model=settings.tts_model,
            voice=settings.tts_voice,
            response_format=tts_format,
        ),
        openai_client=openai_client,
        anthropic_client=anthropic_client,
    )
