"""Casper client: hands-free (wake word) by default, push-to-talk as a fallback.

Hands-free: the microphone stays open. A local voice-activity detector cuts the audio
into utterances; each short utterance is checked for the wake word "Casper" / "Hallo
Casper" by the local backend. After a short confirmation tone the command is recorded
until you stop talking and sent to the backend; the spoken reply is played back.
Right after a reply you can keep talking without the wake word for a few seconds.

Usage:
    python client.py                   # hands-free
    python client.py --push-to-talk    # Enter starts and stops a recording
    python client.py --list-devices    # show audio devices
"""

from __future__ import annotations

import argparse
import collections
import io
import logging
import queue
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
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
FRAME_MS = 30
REQUEST_TIMEOUT_S = 90.0
ALERT_POLL_S = 2.0

# Voice activity detection
DEFAULT_VAD_THRESHOLD = 0.012  # minimum RMS (0..1) that counts as speech
NOISE_FACTOR = 3.0  # speech must be this much louder than the measured background noise
INITIAL_NOISE_FLOOR = 0.004
LISTEN_END_SILENCE_S = 0.8  # pause that ends a (wake) utterance
COMMAND_END_SILENCE_S = 1.2  # pause that ends a command
MIN_SPEECH_S = 0.25  # seconds of actual speech (not pauses or pre-roll) an utterance needs
MAX_WAKE_SEGMENT_S = 15.0
MAX_COMMAND_S = 25.0
COMMAND_START_TIMEOUT_S = 6.0  # how long to wait for the command after the confirmation tone
DEFAULT_FOLLOW_UP_S = 8.0
MIN_PUSH_TO_TALK_S = 0.3

Samples = np.ndarray


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


@dataclass(frozen=True, slots=True)
class WakeResponse:
    wake: bool
    command: str
    transcript: str


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str
    message: str


# ---------------------------------------------------------------------------- voice activity
class UtteranceSegmenter:
    """Cuts a continuous audio stream into utterances using frame energy.

    The background-noise level is tracked while nobody speaks, so the threshold adapts
    to fans, music or a noisy kitchen. Returns finished utterances from :meth:`feed`.
    """

    def __init__(
        self,
        sample_rate: int,
        *,
        end_silence_s: float,
        max_speech_s: float,
        min_threshold: float = DEFAULT_VAD_THRESHOLD,
        min_speech_s: float = MIN_SPEECH_S,
        preroll_s: float = 0.3,
        start_frames: int = 3,
    ) -> None:
        self.frame_size = sample_rate * FRAME_MS // 1000
        self._frame_s = FRAME_MS / 1000
        self._end_silence_frames = max(1, round(end_silence_s / self._frame_s))
        self._max_frames = round(max_speech_s / self._frame_s)
        self._min_speech_frames = round(min_speech_s / self._frame_s)
        self._min_threshold = min_threshold
        self._start_frames = start_frames
        self._preroll: collections.deque[Samples] = collections.deque(
            maxlen=round(preroll_s / self._frame_s) + start_frames
        )
        self._pending = np.zeros(0, dtype=np.float32)
        self.reset()

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def threshold(self) -> float:
        return max(self._min_threshold, self._noise * NOISE_FACTOR)

    def reset(self) -> None:
        """Forget any partial utterance (keeps the learned noise level)."""
        self._noise = getattr(self, "_noise", INITIAL_NOISE_FLOOR)
        self._in_speech = False
        self._voiced_run = 0
        self._voiced_total = 0
        self._silence_run = 0
        self._speech: list[Samples] = []
        self._preroll.clear()
        self._pending = np.zeros(0, dtype=np.float32)

    def feed(self, samples: Samples) -> list[Samples]:
        """Add audio of any length. Returns zero or more finished utterances."""
        self._pending = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        finished: list[Samples] = []
        while len(self._pending) >= self.frame_size:
            frame, self._pending = self._pending[: self.frame_size], self._pending[self.frame_size :]
            utterance = self._process(frame)
            if utterance is not None:
                finished.append(utterance)
        return finished

    def _process(self, frame: Samples) -> Samples | None:
        rms = float(np.sqrt(np.mean(frame * frame)))
        voiced = rms >= self.threshold

        if not self._in_speech:
            self._preroll.append(frame)
            if not voiced:
                self._voiced_run = 0
                self._noise = 0.95 * self._noise + 0.05 * rms  # learn the background level
                return None
            self._voiced_run += 1
            if self._voiced_run >= self._start_frames:
                self._in_speech = True
                self._speech = list(self._preroll)
                self._voiced_total = self._voiced_run
                self._silence_run = 0
            return None

        self._speech.append(frame)
        self._silence_run = 0 if voiced else self._silence_run + 1
        self._voiced_total += int(voiced)
        if self._silence_run >= self._end_silence_frames or len(self._speech) >= self._max_frames:
            return self._finish()
        return None

    def _finish(self) -> Samples | None:
        spoken_frames = self._voiced_total
        # Keep ~0.2 s of the trailing pause so words are not cut off.
        keep = len(self._speech) - max(0, self._silence_run - round(0.2 / self._frame_s))
        audio = np.concatenate(self._speech[:keep])
        self._in_speech = False
        self._voiced_run = 0
        self._voiced_total = 0
        self._silence_run = 0
        self._speech = []
        self._preroll.clear()
        return audio if spoken_frames >= self._min_speech_frames else None


# ---------------------------------------------------------------------------- sounds and playback
def make_tone(parts: list[tuple[float, float]], sample_rate: int, volume: float) -> Samples:
    """Concatenate sine beeps. ``parts`` holds (frequency in Hz, seconds); 0 Hz is a pause."""
    chunks = []
    for frequency, seconds in parts:
        t = np.arange(int(sample_rate * seconds)) / sample_rate
        wave = np.sin(2 * np.pi * frequency * t) if frequency > 0 else np.zeros_like(t)
        fade = min(len(t) // 4, int(0.01 * sample_rate))
        if fade and frequency > 0:
            wave[:fade] *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
        chunks.append(wave * volume)
    return np.concatenate(chunks).astype(np.float32)


class Speaker:
    """Plays sounds and replies one at a time. While it plays, :attr:`busy` is set so the
    microphone loop ignores Casper's own voice."""

    CONFIRM_RATE = 24_000

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.busy = threading.Event()

    def confirm_beep(self) -> None:
        # Short guard time: the user starts talking right after the beep.
        self._play(make_tone([(660, 0.09), (880, 0.12)], self.CONFIRM_RATE, 0.35), self.CONFIRM_RATE, guard_s=0.05)

    def alarm(self, repeats: int = 3) -> None:
        pattern = [(1000, 0.25), (0, 0.12)] * 4 + [(0, 0.4)]
        self._play(make_tone(pattern * repeats, self.CONFIRM_RATE, 0.7), self.CONFIRM_RATE)

    def play_audio(self, audio: bytes) -> None:
        data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
        self._play(data, int(sample_rate))

    def _play(self, data: Samples, sample_rate: int, guard_s: float = 0.25) -> None:
        with self._lock:
            self.busy.set()
            try:
                sd.play(data, sample_rate)
                sd.wait()
            finally:
                time.sleep(guard_s)  # let the room echo fade before listening again
                self.busy.clear()


# ---------------------------------------------------------------------------- backend API
class CasperApi:
    """Thin HTTP client for the Casper backend."""

    def __init__(self, base_url: str, http: httpx.Client) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    def wake_check(self, wav: bytes) -> WakeResponse:
        response = self._http.post(f"{self._base}/wake-check", files={"file": ("segment.wav", wav, "audio/wav")})
        if response.status_code != 200:
            raise ServerError(_error_message(response))
        data = response.json()
        return WakeResponse(bool(data.get("wake")), str(data.get("command", "")), str(data.get("transcript", "")))

    def process_audio(self, wav: bytes, *, strip_wake: bool = False) -> ServerReply:
        response = self._http.post(
            f"{self._base}/process-audio",
            params={"strip_wake": str(strip_wake).lower()},
            files={"file": ("utterance.wav", wav, "audio/wav")},
        )
        return _reply(response)

    def speak(self, text: str) -> bytes:
        response = self._http.post(f"{self._base}/speak", json={"text": text})
        return _reply(response).audio

    def alerts(self) -> list[Alert]:
        response = self._http.get(f"{self._base}/alerts", timeout=5.0)
        response.raise_for_status()
        return [Alert(str(a.get("kind", "")), str(a.get("message", ""))) for a in response.json().get("alerts", [])]


def _reply(response: httpx.Response) -> ServerReply:
    if response.status_code != 200:
        raise ServerError(_error_message(response))
    headers = response.headers
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


def encode_wav(audio: Samples, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


# ---------------------------------------------------------------------------- alerts (timers, alarms)
class AlertPoller(threading.Thread):
    """Fetches fired timers and alarms from the backend and plays them on this device."""

    def __init__(self, api: CasperApi, speaker: Speaker, stop: threading.Event) -> None:
        super().__init__(name="casper-alerts", daemon=True)
        self._api = api
        self._speaker = speaker
        self._stop_event = stop

    def run(self) -> None:
        while not self._stop_event.wait(ALERT_POLL_S):
            try:
                alerts = self._api.alerts()
            except (httpx.HTTPError, ValueError):
                continue  # backend not reachable right now
            for alert in alerts:
                print(f"\n*** {alert.message}")
                try:
                    self._speaker.alarm()
                    self._speaker.play_audio(self._api.speak(alert.message))
                except (httpx.HTTPError, ServerError, RuntimeError, sf.SoundFileError, sd.PortAudioError) as exc:
                    logger.warning("Could not announce alert: %s", exc)


# ---------------------------------------------------------------------------- hands-free mode
class HandsFreeSession:
    """Always-on listening with wake word, command recording and a follow-up window.

    All durations are measured in audio time (frames received), not wall-clock time.
    """

    def __init__(
        self,
        api: CasperApi,
        speaker: Speaker,
        *,
        sample_rate: int,
        device: int | str | None,
        vad_threshold: float,
        follow_up_s: float,
        stream_factory: Callable[..., Any] = sd.InputStream,
    ) -> None:
        self._api = api
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._device = device
        self._follow_up_s = follow_up_s
        self._stream_factory = stream_factory
        self._frames: queue.Queue[Samples] = queue.Queue(maxsize=1000)
        self._listen = UtteranceSegmenter(
            sample_rate, end_silence_s=LISTEN_END_SILENCE_S, max_speech_s=MAX_WAKE_SEGMENT_S, min_threshold=vad_threshold
        )
        self._command = UtteranceSegmenter(
            sample_rate, end_silence_s=COMMAND_END_SILENCE_S, max_speech_s=MAX_COMMAND_S, min_threshold=vad_threshold
        )
        self._frame_s = self._listen.frame_size / sample_rate
        self._frames_seen = 0
        self._follow_until = 0.0

    # --- microphone ---
    def _on_audio(self, indata: Samples, _frames: int, _time: Any, status: Any) -> None:
        if status:
            logger.debug("Audio callback status: %s", status)
        try:
            self._frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # the main loop is busy (e.g. waiting for the server); dropping audio is fine

    def _open_stream(self) -> Any:
        try:
            stream = self._stream_factory(
                samplerate=self._sample_rate,
                channels=CHANNELS,
                dtype="float32",
                blocksize=self._listen.frame_size,
                device=self._device,
                callback=self._on_audio,
            )
            stream.start()
            return stream
        except (sd.PortAudioError, ValueError, OSError) as exc:
            raise MicrophoneError(
                f"Could not open the microphone: {exc}. Run 'python client.py --list-devices' to see "
                "available devices, or use --device <index>."
            ) from exc

    @property
    def _now(self) -> float:
        return self._frames_seen * self._frame_s

    def _next_utterance(
        self, segmenter: UtteranceSegmenter, stop: threading.Event, *, give_up_after_s: float | None = None
    ) -> Samples | None:
        """Block until the segmenter yields an utterance. ``None`` means stop, or nobody spoke in time."""
        started = self._now
        while not stop.is_set():
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue
            self._frames_seen += 1
            if self._speaker.busy.is_set():
                segmenter.reset()  # never listen to our own voice
                started = self._now
                continue
            for utterance in segmenter.feed(frame):
                return utterance
            if give_up_after_s is not None and not segmenter.in_speech and self._now - started > give_up_after_s:
                return None
        return None

    def _flush(self) -> None:
        """Drop buffered audio (our own voice, beeps). Dropped frames still advance the audio clock."""
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break
            self._frames_seen += 1
        self._listen.reset()
        self._command.reset()

    # --- main loop ---
    def run(self, stop: threading.Event) -> None:
        stream = self._open_stream()
        try:
            print(f"Listening. Say 'Hallo Casper' (speech threshold {self._listen.threshold:.3f}). Ctrl+C quits.")
            while not stop.is_set():
                utterance = self._next_utterance(self._listen, stop)
                if utterance is None:
                    continue
                try:
                    self._handle(utterance, stop)
                except httpx.HTTPError as exc:
                    logger.debug("Transport error", exc_info=exc)
                    print("Could not reach the backend. Is server.py running?")
                    time.sleep(1.0)
                except ServerError as exc:
                    print(f"Backend: {exc}")
                self._flush()
        finally:
            stream.stop()
            stream.close()

    def _handle(self, utterance: Samples, stop: threading.Event) -> None:
        wav = encode_wav(utterance, self._sample_rate)

        if self._now < self._follow_until:  # conversation still open: no wake word needed
            self._deliver(self._api.process_audio(wav, strip_wake=True))
            return

        check = self._api.wake_check(wav)
        if not check.wake:
            return
        print(f"Wake word detected ({check.transcript!r}).")
        self._speaker.confirm_beep()

        if check.command:  # "Hallo Casper, wie spät ist es?" in one go
            self._deliver(self._api.process_audio(wav, strip_wake=True))
            return

        self._flush()
        command = self._next_utterance(self._command, stop, give_up_after_s=COMMAND_START_TIMEOUT_S)
        if command is None:
            print("No command heard.")
            return
        self._deliver(self._api.process_audio(encode_wav(command, self._sample_rate), strip_wake=True))

    def _deliver(self, reply: ServerReply) -> None:
        print(f"You said:  {reply.transcript or '(nothing recognised)'}")
        print(f"Casper:    {reply.response_text}")
        try:
            self._speaker.play_audio(reply.audio)
        except (sf.SoundFileError, RuntimeError, ValueError, sd.PortAudioError) as exc:
            print(f"Could not play the reply: {exc}")
        self._flush()
        self._follow_until = self._now + self._follow_up_s


# ---------------------------------------------------------------------------- push-to-talk mode
class PushToTalkRecorder:
    """Records from the microphone between :meth:`start` and :meth:`stop`."""

    def __init__(self, sample_rate: int, device: int | str | None) -> None:
        self._sample_rate = sample_rate
        self._device = device
        self._chunks: list[Samples] = []
        self._lock = threading.Lock()
        self._stream: Any = None

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

    def stop(self) -> Samples:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            chunks = list(self._chunks)
        return np.concatenate(chunks, axis=0) if chunks else np.zeros((0, CHANNELS), dtype=np.float32)

    def _on_audio(self, indata: Samples, _frames: int, _time: Any, status: Any) -> None:
        if status:
            logger.debug("Audio callback status: %s", status)
        with self._lock:
            self._chunks.append(indata.copy())


def _prompt(message: str) -> str:
    return input(message).strip().lower()


def run_push_to_talk(api: CasperApi, speaker: Speaker, sample_rate: int, device: int | str | None) -> int:
    print("Push-to-talk: press Enter to start recording, Enter again to stop. 'q' + Enter quits.")
    recorder = PushToTalkRecorder(sample_rate, device)
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
        if duration < MIN_PUSH_TO_TALK_S:
            print("Recording was too short, nothing sent.")
            continue
        print(f"Sending {duration:.1f} s of audio...")
        try:
            reply = api.process_audio(encode_wav(audio, sample_rate))
        except httpx.HTTPError as exc:
            logger.debug("Transport error", exc_info=exc)
            print("Could not reach the backend. Is server.py running?")
            continue
        except ServerError as exc:
            print(f"Backend: {exc}")
            continue
        print(f"You said:  {reply.transcript or '(nothing recognised)'}")
        print(f"Casper:    {reply.response_text}")
        try:
            speaker.play_audio(reply.audio)
        except (sf.SoundFileError, RuntimeError, ValueError, sd.PortAudioError) as exc:
            print(f"Could not play the reply: {exc}")


# ---------------------------------------------------------------------------- entry point
def main() -> None:
    parser = argparse.ArgumentParser(description="Casper client (hands-free by default).")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL, help="Backend base URL.")
    parser.add_argument("--push-to-talk", action="store_true", help="Use Enter to start/stop recording instead of the wake word.")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE, help="Recording sample rate in Hz (default 16000).")
    parser.add_argument("--device", default=None, help="Input device index or name.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    parser.add_argument("--vad-threshold", type=float, default=DEFAULT_VAD_THRESHOLD, help="Minimum loudness (RMS 0-1) counted as speech. Raise it in noisy rooms.")
    parser.add_argument("--follow-up-seconds", type=float, default=DEFAULT_FOLLOW_UP_S, help="Seconds after a reply in which no wake word is needed (0 = off).")
    parser.add_argument("--no-alerts", action="store_true", help="Do not play timers and alarms from the backend.")
    parser.add_argument("--verbose", action="store_true", help="Show debug output.")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    device: int | str | None = int(args.device) if str(args.device).isdigit() else args.device
    speaker = Speaker()
    stop = threading.Event()

    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as http:
        api = CasperApi(args.server, http)
        print(f"Casper client -> {args.server}")
        if not args.no_alerts:
            AlertPoller(api, speaker, stop).start()

        if args.push_to_talk:
            raise SystemExit(run_push_to_talk(api, speaker, args.sample_rate, device))

        session = HandsFreeSession(
            api,
            speaker,
            sample_rate=args.sample_rate,
            device=device,
            vad_threshold=args.vad_threshold,
            follow_up_s=args.follow_up_seconds,
        )
        try:
            session.run(stop)
        except MicrophoneError as exc:
            print(f"Microphone problem: {exc}\nTip: 'python client.py --push-to-talk' needs a microphone too.")
            raise SystemExit(1) from exc
        except KeyboardInterrupt:
            print("\nBye.")
        finally:
            stop.set()


if __name__ == "__main__":
    main()
