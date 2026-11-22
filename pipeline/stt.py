"""Speech-to-text. The abstract base allows swapping engines; the default is local."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any

from pipeline.errors import TranscriptionError

logger = logging.getLogger(__name__)

BEAM_SIZE = 5
QUICK_BEAM_SIZE = 1
HOTWORDS = "Casper"  # nudges the decoder towards the assistant's name


class SpeechToText(ABC):
    """Converts an audio file into text. Implementations must raise :class:`TranscriptionError`."""

    async def warm_up(self) -> None:  # noqa: B027  # optional hook, default is a no-op
        """Load models ahead of the first request. Safe to skip."""

    @abstractmethod
    async def transcribe(self, audio: bytes, filename: str, *, quick: bool = False) -> str:
        """Return the transcript of ``audio``. ``filename`` carries the format extension.

        ``quick`` trades accuracy for speed (used for the wake-word check).
        """


class FasterWhisperSpeechToText(SpeechToText):
    """Local transcription with ``faster-whisper`` (CTranslate2), no API key needed.

    The model is downloaded on first use and cached by Hugging Face. Inference runs in a
    worker thread so the event loop stays responsive.
    """

    def __init__(self, *, model_size: str, device: str, compute_type: str, language: str) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: Any | None = None
        self._lock = threading.Lock()  # guards loading and serialises inference

    @property
    def description(self) -> str:
        return f"faster-whisper:{self._model_size}/{self._device}/{self._compute_type}"

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._load_model)

    async def transcribe(self, audio: bytes, filename: str, *, quick: bool = False) -> str:
        try:
            return await asyncio.to_thread(self._transcribe_blocking, audio, quick)
        except TranscriptionError:
            raise
        except Exception as exc:
            logger.exception("faster-whisper failed on %s", filename)
            raise TranscriptionError(
                "Die Spracherkennung hat leider einen Fehler gemeldet. Versuch es gleich noch einmal."
            ) from exc

    def _load_model(self) -> Any:
        """Load the model once (thread-safe) and return it."""
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel

                    logger.info("Loading %s (first run downloads the model)...", self.description)
                    self._model = WhisperModel(
                        self._model_size, device=self._device, compute_type=self._compute_type
                    )
                except Exception as exc:
                    logger.exception("Could not load the faster-whisper model")
                    raise TranscriptionError(
                        "Meine Spracherkennung ist noch nicht bereit. Beim ersten Start braucht "
                        "sie eine Internetverbindung für den Modell-Download."
                    ) from exc
            return self._model

    def _transcribe_blocking(self, audio: bytes, quick: bool) -> str:
        model = self._load_model()
        with self._lock:
            segments, _info = model.transcribe(
                io.BytesIO(audio),
                language=self._language,
                beam_size=QUICK_BEAM_SIZE if quick else BEAM_SIZE,
                vad_filter=True,
                hotwords=HOTWORDS,
            )
            # ``segments`` is a lazy generator: decoding happens while iterating.
            return " ".join(segment.text.strip() for segment in segments).strip()
