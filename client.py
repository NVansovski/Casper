"""Laptop test client for Casper.

Press Enter to start recording, press Enter again to stop. The recording is sent
to the backend and the spoken reply is played back immediately.

Usage:
    python client.py                  # talk to http://127.0.0.1:8000
    python client.py --list-devices   # show audio devices
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
import soundfile as sf

try:
    import sounddevice as sd
except OSError as _portaudio_error:  # the native PortAudio library is missing
    sys.exit(
        f"PortAudio is not installed ({_portaudio_error}).\n"
        "Install it and retry: 'sudo apt install libportaudio2' (Debian/Ubuntu) "
        "or 'brew install portaudio' (macOS)."
    )

logger = logging.getLogger("casper.client")

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
DEFAULT_SAMPLE_RATE = 16_000
CHANNELS = 1
MIN_RECORDING_S = 0.3
REQUEST_TIMEOUT_S = 90.0


class MicrophoneError(RuntimeError):
    """No usable microphone, or the input stream could not be opened."""


class ServerError(RuntimeError):
    """The backend returned an error. The message is German and was meant for the user."""


@dataclass(frozen=True, slots=True)
class ServerReply:
    audio: bytes
    status: str
    transcript: str
    response_text: str


class Recorder:
    """Captures microphone audio in a background stream until :meth:`stop` is called."""

    def __init__(self, sample_rate: int, device: int | str | None) -> None:
        self._sample_rate = sample_rate
        self._device = device
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        self._chunks = []
        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=CHANNELS,
                dtype="float32",
                device=self._device,
                callback=self._on_audio,
            )
            self._stream.start()
        except (sd.PortAudioError, ValueError, OSError) as exc:
            self._stream = None
            raise MicrophoneError(
                f"Could not open the microphone: {exc}. Run 'python client.py --list-devices' "
                "to see available devices."
            ) from exc

    def stop(self) -> np.ndarray:
        """Stop capturing and return the audio as a (frames, channels) float32 array."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        return np.concatenate(chunks, axis=0)

    def _on_audio(self, indata: np.ndarray, _frames: int, _time: Any, status: sd.CallbackFlags) -> None:
        if status:
            logger.debug("Audio callback status: %s", status)
        with self._lock:
            self._chunks.append(indata.copy())


def encode_wav(audio: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def send_utterance(http: httpx.Client, server_url: str, wav_bytes: bytes) -> ServerReply:
    """POST the recording to ``/process-audio`` and return the reply.

    Raises:
        httpx.HTTPError: The server is unreachable or the transfer failed.
        ServerError: The server answered with an error.
    """
    response = http.post(
        f"{server_url.rstrip('/')}/process-audio",
        files={"file": ("utterance.wav", wav_bytes, "audio/wav")},
    )
    headers = response.headers
    if response.status_code != 200:
        raise ServerError(_error_message(response))
    return ServerReply(
        audio=response.content,
        status=headers.get("X-Casper-Status", "ok"),
        transcript=urllib.parse.unquote(headers.get("X-Casper-Transcript", "")),
        response_text=urllib.parse.unquote(headers.get("X-Casper-Response", "")),
    )


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Server error (HTTP {response.status_code})."
    message = payload.get("message") or payload.get("detail")
    return str(message) if message else f"Server error (HTTP {response.status_code})."


def play_audio(audio: bytes) -> None:
    """Decode WAV or MP3 bytes and play them through the default output device."""
    data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
    sd.play(data, sample_rate)
    sd.wait()


def _prompt(message: str) -> str:
    return input(message).strip().lower()


def run(server_url: str, sample_rate: int, device: int | str | None) -> int:
    print(f"Casper test client -> {server_url}")
    print("Press Enter to start recording, Enter again to stop. Type 'q' and Enter to quit.")
    recorder = Recorder(sample_rate, device)

    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as http:
        while True:
            try:
                command = _prompt("\n[Enter] start recording  [q] quit > ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if command in {"q", "quit", "exit"}:
                return 0

            try:
                recorder.start()
            except MicrophoneError as exc:
                print(f"Microphone problem: {exc}")
                continue

            try:
                print("Recording... press Enter to stop.")
                _prompt("")
            except (EOFError, KeyboardInterrupt):
                recorder.stop()
                print()
                return 0
            audio = recorder.stop()

            duration = len(audio) / sample_rate
            if duration < MIN_RECORDING_S:
                print("Recording was too short, nothing sent.")
                continue

            print(f"Sending {duration:.1f} s of audio...")
            try:
                reply = send_utterance(http, server_url, encode_wav(audio, sample_rate))
            except httpx.HTTPError as exc:
                logger.debug("Transport error", exc_info=exc)
                print(f"Could not reach the backend at {server_url}. Is server.py running?")
                continue
            except ServerError as exc:
                print(f"Backend: {exc}")
                continue

            print(f"You said:  {reply.transcript or '(nothing recognised)'}")
            print(f"Casper:    {reply.response_text}")
            try:
                play_audio(reply.audio)
            except (sf.SoundFileError, RuntimeError, ValueError, sd.PortAudioError) as exc:
                print(f"Could not play the reply: {exc}")


def list_devices() -> None:
    print(sd.query_devices())


def main() -> None:
    parser = argparse.ArgumentParser(description="Casper laptop test client.")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL, help="Backend base URL.")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Recording sample rate in Hz (default: 16000).",
    )
    parser.add_argument("--device", default=None, help="Input device index or name.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    logging.basicConfig(level=logging.WARNING)
    device: int | str | None = int(args.device) if str(args.device).isdigit() else args.device
    raise SystemExit(run(args.server, args.sample_rate, device))


if __name__ == "__main__":
    main()
