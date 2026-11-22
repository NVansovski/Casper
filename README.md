# Casper

Casper is a modular German voice assistant: a friendly, grounded household
helper that listens, understands, uses tools, and answers out loud.

**Phase 1 (this repo, current):** a FastAPI backend and a CLI test client on
one laptop, using the laptop's microphone and speakers.
**Phase 2 (planned):** an old Android phone as an always-on kitchen client that
talks to the same backend over the local Wi-Fi.

## Architecture

```
 client.py (laptop)                       server.py (FastAPI)
 ┌──────────────────┐   POST /process-audio   ┌───────────────────────────────────────────┐
 │ mic → WAV        │ ──────────────────────▶ │ STT  (OpenAI Whisper, whisper-1, de)      │
 │ speaker ◀ audio  │ ◀────────────────────── │   ↓ transcript                            │
 └──────────────────┘   audio + headers       │ Brain (Anthropic Claude, native tool use) │
                                              │   ↕ ToolRegistry → tools/*.py             │
                                              │   ↓ German reply text                     │
                                              │ TTS  (OpenAI tts-1, voice onyx)           │
                                              └───────────────────────────────────────────┘
```

### Components

| Path | Purpose |
|---|---|
| `server.py` | FastAPI app: `POST /process-audio` and `GET /health` |
| `client.py` | CLI test client: record, send, play back |
| `config.py` | `pydantic-settings` configuration from `.env` and the environment |
| `pipeline/stt.py` | `SpeechToText` interface and the Whisper implementation |
| `pipeline/brain.py` | `CasperBrain`: German persona, Claude tool loop, parallel tool calls |
| `pipeline/tts.py` | `SpeechSynthesizer` interface and the OpenAI TTS implementation |
| `pipeline/orchestrator.py` | `VoicePipeline`: STT → brain → TTS, with spoken error handling |
| `pipeline/errors.py` | `CasperError` family; every message is German and spoken |
| `tools/base.py` | `BaseTool` interface and `ToolError` |
| `tools/registry.py` | `ToolRegistry`: auto-discovers tools and executes calls |
| `tools/weather_tool.py` | Live weather via Open-Meteo (no key) |
| `tools/microsoft_todo_tool.py` | Mock shopping and to-do lists, Graph-shaped |
| `tools/outlook_tool.py` | Mock calendar and unread e-mails, Graph-shaped |
| `tools/powerpoint_tool.py` | Fuzzy-finds a `.pptx` and opens it locally |
| `tools/data_store.py` | Thread-safe, atomic JSON store for `data.json` |
| `tools/text_matching.py` | Umlaut-aware fuzzy matching for voice input |

### Error handling

Casper never answers with a stack trace. Failures become spoken German replies:

- **Missing API key or unreachable service:** the reply names the problem
  ("Ich kann die Spracherkennung gerade nicht erreichen. Prüf bitte die Internetverbindung.").
- **Silence or unintelligible audio:** "Ich habe leider nichts verstanden."
- **Tool failure** (e.g. no matching presentation): the tool returns an error
  object and Claude explains it in German.
- **Only if the TTS service itself is down** is there no audio. The server then
  returns HTTP 503 with a JSON message, and the client prints it.

### Adding a tool

Create `tools/your_tool.py` with a `BaseTool` subclass and a module-level
factory. No changes to `brain.py` or `server.py` are needed.

```python
from typing import Any, ClassVar

from tools.base import BaseTool, ToolError


class EchoTool(BaseTool):
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "Repeat a phrase back to the user."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = str(arguments.get("text", "")).strip()
        if not text:
            raise ToolError("Was soll ich wiederholen?")
        return {"echo": text}


def create_tool() -> BaseTool:
    return EchoTool()
```

Modules without `create_tool` are ignored. A module that fails to import is
logged and skipped, and the rest still load. Check the startup log for
`Registered tool 'echo'`.

## Setup

### 1. Prerequisites

- Python 3.11 or newer (3.13 tested)
- An Anthropic API key and an OpenAI API key
- **Linux only:** PortAudio for the microphone and speakers, for example
  `sudo apt install libportaudio2`. Windows and macOS wheels include it.

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

### 3. Configure `.env`

Open `.env` and set at least:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `PPTX_SEARCH_DIR`, if you want the presentation tool (the folder is searched recursively)
- `DEFAULT_CITY`, if not Frankfurt am Main

### 4. Run

Terminal 1, the backend:

```bash
uvicorn server:app --host 127.0.0.1 --port 8000
```

Check it: `curl http://127.0.0.1:8000/health`

Terminal 2, the microphone client:

```bash
python client.py
```

Press Enter to start recording, speak, press Enter to stop. Casper replies
through your speakers. Use `python client.py --list-devices` to find a
microphone, and `--device <index>` or `--sample-rate 44100` if the default
device does not work.

### Example commands

- "Wie wird das Wetter heute in Berlin?"
- "Setz Milch und Eier auf die Einkaufsliste."
- "Hake Brot auf der Einkaufsliste ab."
- "Was steht heute in meinem Kalender?"
- "Habe ich ungelesene E-Mails?"
- "Trag morgen um fünfzehn Uhr Zahnarzt ein."
- "Öffne die Präsentation Quartalsbericht Q3."

## Mock data

The Microsoft To Do and Outlook tools use `data.json` in the project root. It is
created on first use with sample lists, events, and unread e-mails. To reset it,
delete the file. Seeded dates are relative to the moment of creation. The file
is git-ignored.

## Migration to Microsoft Graph (Phase 1.5)

The mock tools expose the same models and method names as the Graph resources:

| Mock (`TodoBackend` / `OutlookBackend`) | Microsoft Graph |
|---|---|
| `list_task_lists()` | `GET /me/todo/lists` |
| `list_tasks(list_id)` | `GET /me/todo/lists/{id}/tasks` |
| `create_task(list_id, title)` | `POST /me/todo/lists/{id}/tasks` |
| `update_task_status(...)` | `PATCH /me/todo/lists/{id}/tasks/{taskId}` |
| `delete_task(...)` | `DELETE /me/todo/lists/{id}/tasks/{taskId}` |
| `list_events(start, end)` | `GET /me/calendarView` |
| `create_event(...)` | `POST /me/events` |
| `list_messages(...)` | `GET /me/mailFolders/inbox/messages` |

To go live, write `GraphTodoBackend(TodoBackend)` and `GraphOutlookBackend(OutlookBackend)`
using `MS_CLIENT_ID` and `MS_TENANT_ID`, then change only the `create_tool()`
factories. The LLM-facing tool definitions do not change.

## Phase 2 notes

- The backend binds to `127.0.0.1` by default. For the phone on the LAN, run
  `uvicorn server:app --host 0.0.0.0 --port 8000` and protect the endpoint. It
  has no authentication yet.
- The PowerPoint tool opens files on the machine that runs the backend. On a
  headless server it can find a file but cannot open it.
- The STT and TTS classes are interfaces. A local `faster-whisper` engine only
  needs a new `SpeechToText` subclass and a change in `build_pipeline`.
