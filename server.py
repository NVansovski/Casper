"""Casper backend: audio in, spoken German audio out.

Run with:
    uvicorn server:app --host 127.0.0.1 --port 8000
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

from config import get_settings
from pipeline.errors import CasperError
from pipeline.orchestrator import VoicePipeline, build_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("casper.server")

HEADER_STATUS = "X-Casper-Status"
HEADER_TRANSCRIPT = "X-Casper-Transcript"
HEADER_RESPONSE = "X-Casper-Response"


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
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY is not set: speech recognition and synthesis will fail.")
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY is not set: reasoning will fail.")

    app.state.pipeline = build_pipeline(settings)
    logger.info("%s backend ready. Tools: %s", settings.assistant_name, ", ".join(app.state.pipeline.tool_names))
    yield
    await app.state.pipeline.aclose()
    logger.info("Backend stopped.")


app = FastAPI(title="Casper Backend", version="0.1.0", lifespan=lifespan)


def _pipeline(request: Request) -> VoicePipeline:
    pipeline: VoicePipeline = request.app.state.pipeline
    return pipeline


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness and configuration check. Reports whether keys are set, never their values."""
    settings = get_settings()
    return {
        "status": "ok",
        "assistant": settings.assistant_name,
        "tools": _pipeline(request).tool_names,
        "anthropic_key_configured": bool(settings.anthropic_api_key),
        "openai_key_configured": bool(settings.openai_api_key),
    }


@app.post("/process-audio")
async def process_audio(request: Request, file: UploadFile = File(...)) -> Response:
    """Accept a WAV or MP3 utterance and return Casper's spoken reply as audio.

    Response headers (percent-encoded UTF-8):
        X-Casper-Status: ``ok`` or ``error``
        X-Casper-Transcript: what Casper understood
        X-Casper-Response: the text that was spoken
    """
    settings = get_settings()
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Die Aufnahme ist zu groß.")
    if not data:
        raise HTTPException(status_code=400, detail="Die Aufnahme ist leer.")

    audio_format = detect_audio_format(data)
    if audio_format is None:
        raise HTTPException(status_code=415, detail="Nur WAV- und MP3-Dateien werden unterstützt.")

    try:
        result = await _pipeline(request).process(data, f"utterance.{audio_format}")
    except CasperError as exc:
        logger.error("Could not produce speech: %s", exc.spoken_message)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": exc.spoken_message},
        )
    except Exception:
        logger.exception("Unhandled error while processing audio")
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


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Casper backend.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for LAN clients).")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
