"""Wires STT, brain and TTS into one request-scoped voice pipeline."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from config import Settings
from pipeline.brain import CasperBrain
from pipeline.errors import CasperError, ConfigurationError, TranscriptionError
from pipeline.stt import FasterWhisperSpeechToText, SpeechToText
from pipeline.tts import EdgeSpeechSynthesizer, SpeechSynthesizer
from pipeline.wake import detect_wake_word
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


@dataclass(frozen=True, slots=True)
class WakeCheck:
    """Result of the wake-word check on a short speech segment."""

    detected: bool
    transcript: str
    command: str


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

    async def process(self, audio: bytes, filename: str, *, strip_wake: bool = False) -> PipelineResult:
        """Audio in, spoken reply out. ``strip_wake`` removes a leading "Hallo Casper"."""

        async def transcribe() -> str:
            transcript = await self._stt.transcribe(audio, filename)
            if strip_wake:
                wake = detect_wake_word(transcript)
                if wake.detected:
                    transcript = wake.command
            return transcript

        return await self._answer(transcribe)

    async def process_text(self, text: str) -> PipelineResult:
        """Text in (already transcribed), spoken reply out."""

        async def passthrough() -> str:
            return text.strip()

        return await self._answer(passthrough)

    async def speak(self, text: str) -> PipelineResult:
        """Speak ``text`` as is (timer and alarm announcements). Only TTS is involved."""
        audio = await self._tts.synthesize(text)
        return PipelineResult("ok", "", text, audio, self._tts.media_type)

    async def check_wake_word(self, audio: bytes, filename: str) -> WakeCheck:
        """Quickly transcribe a short segment and look for the wake word. Never raises."""
        try:
            transcript = await self._stt.transcribe(audio, filename, quick=True)
        except CasperError as exc:
            logger.warning("Wake check skipped: %s", exc.spoken_message)
            return WakeCheck(False, "", "")
        except Exception:
            logger.exception("Wake check failed")
            return WakeCheck(False, "", "")
        result = detect_wake_word(transcript)
        logger.info("Wake check: %r -> %s", transcript, "WAKE" if result.detected else "ignored")
        return WakeCheck(result.detected, transcript, result.command)

    async def _answer(self, get_transcript: Callable[[], Awaitable[str]]) -> PipelineResult:
        """Transcribe, reason, synthesise. Only a TTS failure is raised to the caller."""
        transcript = ""
        status: Literal["ok", "error"] = "ok"
        try:
            self._ensure_configured()
            transcript = await get_transcript()
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
