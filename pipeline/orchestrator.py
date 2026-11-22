"""Wires STT, brain and TTS into one request-scoped voice pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from config import Settings
from pipeline.brain import CasperBrain
from pipeline.errors import CasperError, ConfigurationError, TranscriptionError
from pipeline.stt import FasterWhisperSpeechToText, SpeechToText
from pipeline.tts import EdgeSpeechSynthesizer, SpeechSynthesizer
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

ANTHROPIC_TIMEOUT_S = 45.0

GENERIC_ERROR = "Da ist leider etwas schiefgegangen. Versuch es bitte noch einmal."
EMPTY_TRANSCRIPT = "Ich habe leider nichts verstanden. Sprich bitte etwas deutlicher."
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
        anthropic_client: AsyncAnthropic,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._brain = brain
        self._tts = tts
        self._anthropic_client = anthropic_client

    @property
    def tool_names(self) -> list[str]:
        return self._brain.tool_names

    async def warm_up(self) -> None:
        """Preload the speech model so the first utterance is not slow."""
        await self._stt.warm_up()

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
        """Release HTTP connections held by the Anthropic client."""
        await self._anthropic_client.close()

    def _ensure_configured(self) -> None:
        if not self._settings.anthropic_api_key:
            raise ConfigurationError(MISSING_ANTHROPIC_KEY)


def build_pipeline(settings: Settings) -> VoicePipeline:
    """Construct the production pipeline. Missing keys are reported per request, not at startup."""
    anthropic_client = AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=ANTHROPIC_TIMEOUT_S
    )
    registry = ToolRegistry.discover()
    timezone = ZoneInfo(settings.timezone)

    return VoicePipeline(
        settings,
        stt=FasterWhisperSpeechToText(
            model_size=settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
            language=settings.stt_language,
        ),
        brain=CasperBrain(
            anthropic_client,
            registry,
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            assistant_name=settings.assistant_name,
            timezone=timezone,
        ),
        tts=EdgeSpeechSynthesizer(voice=settings.tts_voice),
        anthropic_client=anthropic_client,
    )
