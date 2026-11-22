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
 │ mic → WAV        │ ──────────────────────▶ │ STT  (local faster-whisper, de, int8)     │
 │ speaker ◀ audio  │ ◀────────────────────── │   ↓ transcript                            │
 └──────────────────┘   audio + headers       │ Brain (Anthropic Claude, native tool use) │
                                              │   ↕ ToolRegistry → tools/*.py             │
                                              │   ↓ German reply text                     │
                                              │ TTS  (edge-tts, de-DE-ConradNeural, MP3)  │
                                              └───────────────────────────────────────────┘
```

### Components

| Path | Purpose |
|---|---|
| `server.py` | FastAPI app: `POST /process-audio` and `GET /health` |
| `client.py` | CLI test client: record, send, play back |
| `config.py` | `pydantic-settings` configuration from `.env` and the environment |
| `pipeline/stt.py` | `SpeechToText` interface and the local `faster-whisper` implementation |
| `pipeline/brain.py` | `CasperBrain`: German persona, Claude tool loop, parallel tool calls |
| `pipeline/tts.py` | `SpeechSynthesizer` interface and the `edge-tts` implementation |
| `pipeline/orchestrator.py` | `VoicePipeline`: STT → brain → TTS, with spoken error handling |
| `pipeline/errors.py` | `CasperError` family; every message is German and spoken |
| `tools/base.py` | `BaseTool` interface and `ToolError` |
| `tools/registry.py` | `ToolRegistry`: auto-discovers tools and executes calls |
| `tools/weather_tool.py` | `get_weather`: current weather and 7-day forecast via Open-Meteo (no key) |
| `tools/ms_calendar_tool.py` | `get_calendar_events`, `create_calendar_event` (Microsoft Graph) |
| `tools/ms_todo_tool.py` | `get_todo_lists`, `add_todo_item` (Microsoft Graph) |
| `tools/ms_mail_tool.py` | `get_unread_emails` (Microsoft Graph, read-only) |
| `tools/graph_client.py` | MSAL device-code sign-in, token cache, Graph REST client |
| `auth_microsoft.py` | One-time Microsoft sign-in script (German output) |
| `tools/microsoft_todo_tool.py`, `tools/outlook_tool.py` | Local mock lists, calendar and mail, used only without `MS_CLIENT_ID` |
| `tools/powerpoint_tool.py` | Fuzzy-finds a `.pptx` and opens it locally |
| `tools/data_store.py` | Thread-safe, atomic JSON store for `data.json` |
| `tools/text_matching.py` | Umlaut-aware fuzzy matching for voice input |

### Error handling

Casper never answers with a stack trace. Failures become spoken German replies:

- **Missing API key or unreachable service:** the reply names the problem
  ("Ich kann mein Sprachmodell gerade nicht erreichen. Prüf bitte die Internetverbindung.").
- **Silence or unintelligible audio:** "Ich habe leider nichts verstanden."
- **Tool failure** (e.g. no matching presentation): the tool returns an error
  object and Claude explains it in German.
- **Only if the TTS service itself is down** is there no audio. The server then
  returns HTTP 503 with a JSON message, and the client prints it.

### Adding a tool

Create `tools/your_tool.py` with a `BaseTool` subclass and a module-level
factory: `create_tool()` for one tool (return `None` to stay disabled) or
`create_tools()` for a list. No changes to `brain.py` or `server.py` are needed.

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
- An Anthropic API key. It is the only key needed: speech recognition runs
  locally and the voice comes from `edge-tts`.
- Internet access. Claude and `edge-tts` are online services, and the
  speech model is downloaded once.
- **Linux only:** PortAudio for the microphone and speakers, for example
  `sudo apt install libportaudio2`. Windows and macOS wheels include it.

**First start:** `faster-whisper` downloads its model on first launch (about
0.5 GB for `small`, 0.15 GB for `base`) and caches it. The backend loads it at
startup, so wait for the "backend ready" log line before speaking. Set
`STT_MODEL=base` in `.env` for faster, slightly less accurate recognition on
weak CPUs. The reply is MP3, which the client decodes with `soundfile`.

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
- `PPTX_SEARCH_DIR`, if you want the presentation tool (the folder is searched recursively)
- `DEFAULT_CITY` (default `Ulm`): used for the weather whenever you name no place
- `MS_CLIENT_ID` (and `MS_TENANT_ID` for work accounts), to use your real Microsoft
  account; see [Microsoft account](#microsoft-account)

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

- "Wie wird das Wetter morgen?" (uses `DEFAULT_CITY`)
- "Wird es am Wochenende regnen?" / "Wie wird das Wetter übermorgen in Berlin?"
- "Setz Milch und Eier auf die Einkaufsliste."
- "Hake Brot auf der Einkaufsliste ab."
- "Was steht heute in meinem Kalender?"
- "Habe ich ungelesene E-Mails?"
- "Trag morgen um fünfzehn Uhr Zahnarzt ein."
- "Öffne die Präsentation Quartalsbericht Q3."

## Microsoft account

With `MS_CLIENT_ID` set, Casper uses your real Microsoft account through the
Microsoft Graph API. These tools become available to the assistant:

| Tool | What it does | Graph call |
|---|---|---|
| `get_calendar_events` | Appointments for today, tomorrow or a date range | `GET /me/calendarView` |
| `create_calendar_event` | Creates an appointment | `POST /me/events` |
| `get_todo_lists` | Lists To Do lists, optionally with a list's open items | `GET /me/todo/lists` |
| `add_todo_item` | Adds an item, e.g. to the "Einkaufsliste" (fuzzy list name) | `POST /me/todo/lists/{id}/tasks` |
| `get_unread_emails` | The newest unread e-mails: sender, subject, short preview | `GET /me/mailFolders/inbox/messages` |

Delegated permissions: `Calendars.ReadWrite`, `Tasks.ReadWrite`, `Mail.Read`, `User.Read`.
E-mail is strictly read-only, and Casper treats e-mail text as data, never as instructions.

### One-time setup

1. Register an app at <https://entra.microsoft.com> under *App registrations*.
   Choose the account type that matches you (personal accounts need "personal
   Microsoft accounts" enabled).
2. Under *Authentication*, set **Allow public client flows** to **Yes**.
3. Under *API permissions*, add the four delegated Microsoft Graph permissions above.
4. Put the *Application (client) ID* into `.env` as `MS_CLIENT_ID`. For a work
   or school account also set `MS_TENANT_ID` (the *Directory (tenant) ID*).
   Leave it empty for personal accounts.
5. Sign in once. The script prints a link and a code in German:

   ```bash
   python auth_microsoft.py
   ```

   Open the link in any browser, enter the code, and confirm. The tokens are
   stored in `.msal_cache.json` (git-ignored, owner-only permissions where
   supported). Casper renews them silently afterwards. `python auth_microsoft.py --logout`
   deletes them.
6. Restart the backend.

If a token expires or the sign-in is missing, Casper says so in German and asks you
to run `auth_microsoft.py` again. Keep `.msal_cache.json` private: it grants access to
your calendar, tasks and mail.

### Without a Microsoft app (mock data)

If `MS_CLIENT_ID` is empty, Casper uses local stand-ins instead: the
`microsoft_todo` and `outlook` tools read and write `data.json` in the project
root. It is created on first use with sample lists, events and unread e-mails.
Delete the file to reset it. Seeded dates are relative to the moment of creation.
Mock and real tools are never active together, so the assistant never mixes the
two data sources.

## Phase 2 notes

- The backend binds to `127.0.0.1` by default. For the phone on the LAN, run
  `uvicorn server:app --host 0.0.0.0 --port 8000` and protect the endpoint. It
  has no authentication yet. **With a Microsoft account connected, anyone who can
  reach the backend can read your calendar, tasks and mail through Casper**, so add
  authentication (and keep it off untrusted networks) before exposing it.
- The PowerPoint tool opens files on the machine that runs the backend. On a
  headless server it can find a file but cannot open it.
- The STT and TTS classes are interfaces. Another engine only needs a new
  `SpeechToText` or `SpeechSynthesizer` subclass and a change in `build_pipeline`.
- `edge-tts` uses Microsoft's online Edge voices without a key. It is an
  unofficial interface, so it may change or be rate-limited.
