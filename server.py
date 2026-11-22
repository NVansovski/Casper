"""Casper backend: audio in, spoken German audio out.

Run with:
    uvicorn server:app --host 127.0.0.1 --port 8000

Endpoints:
    GET  /health         liveness and configuration
    POST /wake-check     short audio segment -> was the wake word said?
    POST /process-audio  utterance (WAV/MP3) -> spoken reply (audio)
    POST /process-text   utterance as text -> spoken reply (audio)
    POST /speak          text -> audio (timer and alarm announcements)
    GET  /alerts         fired timers and alarms, for the client to play
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from config import Settings, get_settings
from pipeline.errors import CasperError
from pipeline.orchestrator import PipelineResult, VoicePipeline, build_pipeline
from tools.alerts import get_alert_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("casper.server")

HEADER_STATUS = "X-Casper-Status"
HEADER_TRANSCRIPT = "X-Casper-Transcript"
HEADER_RESPONSE = "X-Casper-Response"


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def detect_audio_format(data: bytes) -> str | None:
    """Identify WAV or MP3 from magic bytes. Returns ``None`` for anything else."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"  # MPEG frame sync
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY is not set: reasoning will fail.")

    app.state.pipeline = build_pipeline(settings)
    try:
        await app.state.pipeline.warm_up()
    except CasperError as exc:
        # Not fatal: the model is retried on the first request.
        logger.warning("Speech model not ready yet: %s", exc.spoken_message)
    logger.info("%s backend ready. Tools: %s", settings.assistant_name, ", ".join(app.state.pipeline.tool_names))
    yield
    get_alert_service().stop()
    await app.state.pipeline.aclose()
    logger.info("Backend stopped.")


app = FastAPI(title="Casper Backend", version="0.2.0", lifespan=lifespan)


def _pipeline(request: Request) -> VoicePipeline:
    pipeline: VoicePipeline = request.app.state.pipeline
    return pipeline


async def _read_audio(file: UploadFile, settings: Settings) -> tuple[bytes, str]:
    """Read and validate an uploaded recording. Returns the bytes and ``wav``/``mp3``."""
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Die Aufnahme ist zu groß.")
    if not data:
        raise HTTPException(status_code=400, detail="Die Aufnahme ist leer.")
    audio_format = detect_audio_format(data)
    if audio_format is None:
        raise HTTPException(status_code=415, detail="Nur WAV- und MP3-Dateien werden unterstützt.")
    return data, audio_format


async def _respond(produce: Any) -> Response:
    """Run a pipeline call and wrap its result (or a TTS failure) in an HTTP response."""
    try:
        result: PipelineResult = await produce()
    except CasperError as exc:
        logger.error("Could not produce speech: %s", exc.spoken_message)
        return JSONResponse(status_code=503, content={"status": "error", "message": exc.spoken_message})
    except Exception:
        logger.exception("Unhandled error while processing a request")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Da ist leider etwas schiefgegangen."},
        )
    return Response(
        content=result.audio,
        media_type=result.media_type,
        headers={
            HEADER_STATUS: result.status,
            HEADER_TRANSCRIPT: quote(result.transcript, safe=""),
            HEADER_RESPONSE: quote(result.response_text, safe=""),
        },
    )


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness and configuration check. Reports whether keys are set, never their values."""
    settings = get_settings()
    return {
        "status": "ok",
        "assistant": settings.assistant_name,
        "tools": _pipeline(request).tool_names,
        "anthropic_key_configured": bool(settings.anthropic_api_key),
        "stt_model": f"faster-whisper:{settings.stt_model}",
        "tts_voice": settings.tts_voice,
        "microsoft_connected_app": settings.graph_enabled,
    }


@app.post("/wake-check")
async def wake_check(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """Was the wake word ("Casper", "Hallo Casper") said in this short speech segment?

    The audio is transcribed locally and is not sent to any cloud service.
    """
    data, audio_format = await _read_audio(file, get_settings())
    result = await _pipeline(request).check_wake_word(data, f"segment.{audio_format}")
    return {"wake": result.detected, "transcript": result.transcript, "command": result.command}


@app.post("/process-audio")
async def process_audio(request: Request, file: UploadFile = File(...), strip_wake: bool = False) -> Response:
    """Accept a WAV or MP3 utterance and return Casper's spoken reply as audio.

    ``strip_wake=true`` removes a leading wake word from the transcript first.

    Response headers (percent-encoded UTF-8):
        X-Casper-Status: ``ok`` or ``error``
        X-Casper-Transcript: what Casper understood
        X-Casper-Response: the text that was spoken
    """
    data, audio_format = await _read_audio(file, get_settings())
    pipeline = _pipeline(request)
    return await _respond(lambda: pipeline.process(data, f"utterance.{audio_format}", strip_wake=strip_wake))


@app.post("/process-text")
async def process_text(request: Request, body: TextRequest) -> Response:
    """Like ``/process-audio`` for an utterance that is already text."""
    pipeline = _pipeline(request)
    return await _respond(lambda: pipeline.process_text(body.text))


@app.post("/speak")
async def speak(request: Request, body: SpeakRequest) -> Response:
    """Synthesise ``text`` without involving the LLM."""
    pipeline = _pipeline(request)
    return await _respond(lambda: pipeline.speak(body.text))


@app.get("/alerts")
async def alerts() -> dict[str, Any]:
    """Return (and clear) fired timers and alarms. The client plays them on its speakers."""
    return {"alerts": get_alert_service().pop_alerts()}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Casper backend.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for LAN clients).")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
