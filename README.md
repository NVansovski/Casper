# Casper

Casper is a modular German voice assistant: a friendly, grounded household
helper that listens for its name, understands you, uses tools (calendar, lists,
mail, trains, web, timers, ...), and answers out loud.

**Phase 1 (this repo, current):** a FastAPI backend and a client on one laptop,
using the laptop's microphone and speakers.
**Phase 2 (planned):** an old Android phone as an always-on kitchen client that
talks to the same backend over the local Wi-Fi.

## Architecture

```
 client.py (laptop)                            server.py (FastAPI)
 ┌───────────────────────┐  POST /wake-check   ┌───────────────────────────────────────────┐
 │ mic → local VAD       │ ──────────────────▶ │ STT  (local faster-whisper, de, int8)     │
 │ "Hallo Casper"? beep  │  POST /process-audio│   ↓ transcript                            │
 │ record until pause    │ ──────────────────▶ │ Brain (Anthropic Claude, native tool use, │
 │ speaker ◀ reply       │ ◀────────────────── │        short-term memory)                 │
 │ speaker ◀ alarm sound │  GET /alerts        │   ↕ ToolRegistry → tools/*.py             │
 └───────────────────────┘ ◀────────────────── │   ↓ German reply text                     │
                                               │ TTS  (edge-tts, de-DE-ConradNeural, MP3)  │
                                               └───────────────────────────────────────────┘
```

### What Casper can do

| Area | Tools | Needs |
|---|---|---|
| Hands-free use | wake word "Casper" / "Hallo Casper", `--push-to-talk` fallback | microphone |
| Weather | `get_weather` (current + 7 days, default city `Ulm`) | nothing |
| Microsoft calendar | `get_calendar_events`, `create_calendar_event` | Microsoft app, `auth_microsoft.py` |
| Microsoft To Do | `get_todo_lists`, `add_todo_item`, `complete_todo_item`, `update_todo_item`, `delete_todo_item`, `get_todo_view` ("Mein Tag", "Wichtig") | same |
| Microsoft mail | `get_unread_emails` (read-only) | same |
| OneNote | `search_onenote_pages`, `create_onenote_page` | same |
| Gmail | `get_gmail_messages`, `search_gmail`, `send_gmail` (asks first) | `credentials.json`, `auth_google.py` |
| Google apps | `open_google_app`, `control_presentation` ("Nächste Folie") | a desktop session |
| Trains / transit | `get_train_connections` (live, platform, delay) | nothing |
| Web | `web_search`, `wikipedia_summary`, `search_products`, `get_news` | nothing |
| Meal plan, recipes | `familywall_meals`, `familywall_recipes` | local store, see [limits](#what-is-real-and-what-is-not) |
| REWE | `search_rewe_products`, `rewe_cart` | local cart, see [limits](#what-is-real-and-what-is-not) |
| Timers, alarms | `set_timer`, `set_alarm`, `list_timers_and_alarms`, `cancel_timer_or_alarm` | client speakers |
| Daily briefing | `get_daily_briefing` ("Guten Morgen", "Tagesüberblick") | uses the tools above |
| Presentations | `find_presentation` (opens a local `.pptx`) | `PPTX_SEARCH_DIR` |

Without a Microsoft app, local mock tools (`microsoft_todo`, `outlook`) keep the
calendar, lists and mail usable for testing.

### What is real and what is not

Be aware of these limits before relying on a feature:

- **FamilyWall has no public API.** The meal plan and recipe box are a **local**
  store in `data.json` with the same concepts (planned meals, recipes; add,
  change, delete). It does not sync with the FamilyWall app. FamilyWall
  *events* are not supported. A real adapter would have to replace the
  `FamilyWallStore` class in `tools/familywall_tool.py`.
- **REWE has no public API for its shop.** Product search runs through a web
  search restricted to rewe.de (name, price, link; prices come from search
  snippets and can be stale). The **cart is local**: Casper can add, change
  quantities, remove and clear. `rewe_cart open` opens the product pages and
  the REWE basket page in your browser, where you put items into the real
  basket and order. Casper never logs in to REWE.
- **"Mein Tag"** is approximated: Microsoft Graph does not expose the My Day
  flag, so the view shows open tasks that are due today or overdue, across all
  lists. "Wichtig" uses the real importance flag (`importance eq 'high'`).
- **Wake word** detection uses the local Whisper model, not a dedicated wake
  word engine (`openwakeword` has no pre-trained "Casper" model). It is
  accurate enough for home use but costs some CPU, and a sentence that merely
  *starts* with the name ("Kasper hat Hunger") also triggers it.
- **Timers** live in memory and are lost when the backend restarts. **Alarms**
  are stored and survive a restart; an alarm that was missed while the
  backend was off is skipped.
- **Slide control** presses keys on the computer that runs the backend, in
  whichever window has focus. It works on Windows, macOS and X11 Linux, not on
  Wayland or a headless server.
- Some integrations were tested against simulated services only (see the end).

## Components

| Path | Purpose |
|---|---|
| `server.py` | FastAPI app: `/health`, `/wake-check`, `/process-audio`, `/process-text`, `/speak`, `/alerts` |
| `client.py` | Hands-free client (VAD, wake word, alarms) and push-to-talk fallback |
| `config.py` | `pydantic-settings` configuration from `.env` and the environment |
| `auth_microsoft.py`, `auth_google.py` | One-time sign-in scripts (German output) |
| `pipeline/stt.py` | `SpeechToText` interface and the local `faster-whisper` implementation |
| `pipeline/wake.py` | Fuzzy wake-word detection on a transcript |
| `pipeline/brain.py` | `CasperBrain`: German persona, Claude tool loop, parallel tool calls, short-term memory |
| `pipeline/tts.py` | `SpeechSynthesizer` interface and the `edge-tts` implementation |
| `pipeline/orchestrator.py` | `VoicePipeline`: STT → brain → TTS, spoken error handling |
| `tools/registry.py`, `tools/base.py` | Tool auto-discovery and the `BaseTool` interface |
| `tools/graph_client.py` | MSAL device-code sign-in, token cache, Graph REST client |
| `tools/ms_*_tool.py` | Calendar, To Do, mail and OneNote tools (Microsoft Graph) |
| `tools/gmail_tool.py`, `tools/google_client.py` | Gmail tools and Google OAuth helpers |
| `tools/google_apps_tool.py` | Open Google apps, control slideshows |
| `tools/transit_tool.py` | Train connections via `v6.db.transport.rest` |
| `tools/web_search_tool.py` | DuckDuckGo search, Wikipedia, products, news |
| `tools/familywall_tool.py`, `tools/rewe_tool.py` | Meal plan, recipes, REWE search and cart |
| `tools/daily_helpers_tool.py`, `tools/alerts.py` | Timers, alarms, daily briefing, alert scheduler |
| `tools/weather_tool.py`, `tools/powerpoint_tool.py` | Weather, `.pptx` finder |
| `tools/microsoft_todo_tool.py`, `tools/outlook_tool.py` | Local mock lists, calendar, mail (only without `MS_CLIENT_ID`) |
| `tools/data_store.py`, `tools/text_matching.py`, `tools/time_utils.py`, `tools/context.py` | Shared helpers |

## Setup

### 1. Prerequisites

- Python 3.11 or newer (3.13 tested)
- An Anthropic API key. It is the only key needed: speech recognition runs
  locally, the voice comes from `edge-tts`, search needs no key.
- Internet access (Claude, `edge-tts`, web, trains). The speech model is
  downloaded once.
- **Linux only:** PortAudio for the microphone and speakers, for example
  `sudo apt install libportaudio2`. Windows and macOS wheels include it.

**First start:** `faster-whisper` downloads its model on first launch (about
0.5 GB for `small`, 0.15 GB for `base`) and caches it. The backend loads it at
startup, so wait for the "backend ready" log line before speaking. Use
`STT_MODEL=base` for faster, slightly less accurate recognition on weak CPUs.

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

### 3. Configure `.env`

Set at least `ANTHROPIC_API_KEY`. Everything else is optional:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | **Required.** Reasoning and tool use |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | Model for the assistant |
| `DEFAULT_CITY` | `Ulm` | Weather place, and start station for trains, when you name none |
| `STT_MODEL`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `STT_LANGUAGE` | `small`, `cpu`, `int8`, `de` | Local speech recognition |
| `TTS_VOICE` | `de-DE-ConradNeural` | `edge-tts` voice (also `de-DE-KillianNeural`) |
| `MS_CLIENT_ID`, `MS_TENANT_ID` | empty | Microsoft app; empty = local mock tools |
| `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE` | `credentials.json`, `token_google.json` | Gmail OAuth files |
| `PPTX_SEARCH_DIR` | empty | Folder searched for `.pptx` files |
| `SEARCH_REGION` | `de-de` | DuckDuckGo region |
| `TRANSIT_API_URL` | `https://v6.db.transport.rest` | Train API |
| `TIMEZONE` | `Europe/Berlin` | Dates, calendar, alarms |

### 4. Run

Terminal 1, the backend:

```bash
uvicorn server:app --host 127.0.0.1 --port 8000
```

Terminal 2, the client (hands-free):

```bash
python client.py
```

Check the backend with `curl http://127.0.0.1:8000/health`.

## Hands-free client

`python client.py` keeps the microphone open and needs no key press:

1. A local voice-activity detector (frame energy with an adaptive noise floor)
   cuts the audio into utterances.
2. Each short utterance goes to `POST /wake-check`. The backend transcribes it
   **locally** and looks for "Casper" or "Hallo Casper" at the start. Audio
   without the wake word is discarded; nothing goes to a cloud service.
3. On a hit, a short confirmation tone plays. If you said the command in the
   same breath ("Hallo Casper, wie wird das Wetter?") it is processed at once;
   otherwise Casper records until you pause.
4. The spoken reply plays. For a few seconds afterwards you can keep talking
   **without** the wake word (useful for follow-ups and confirmations).
5. Casper ignores the microphone while it speaks, so it does not hear itself.
   Timers and alarms play a clear alarm sound plus a spoken message.

Options:

| Option | Meaning |
|---|---|
| `--push-to-talk` | Enter starts and stops a recording (no wake word) |
| `--vad-threshold 0.02` | Minimum loudness counted as speech. Raise it in noisy rooms (default `0.012`) |
| `--follow-up-seconds 8` | Time after a reply in which no wake word is needed (`0` = off) |
| `--no-alerts` | Do not play timers and alarms |
| `--device N`, `--sample-rate 44100`, `--list-devices` | Audio setup |
| `--server URL`, `--verbose` | Backend address, debug output |

If Casper does not react, run with `--verbose`, check `--list-devices`, and
speak clearly right after the name. If it reacts to the TV, raise `--vad-threshold`.

### Example commands

- "Hallo Casper, wie wird das Wetter am Wochenende?"
- "Guten Morgen" / "Tagesüberblick" / "Was gibt es Neues in der Welt?"
- "Was steht in Mein Tag?" / "Was ist wichtig?"
- "Hake Brot auf der Einkaufsliste ab." / "Verschiebe Zahnarzt auf morgen."
- "Suche in OneNote nach Urlaub." / "Leg eine Notiz Ideen an: ..."
- "Habe ich ungelesene Mails?" / "Schick Anna eine Mail, dass ich später komme."
- "Wann fährt der nächste Zug nach München?" (starts at `DEFAULT_CITY`)
- "Wer ist Angela Merkel?" / "Was kostet der Sony WH-1000XM5?"
- "Was gibt es heute zu essen?" / "Trag am Freitag Lasagne ein."
- "Such Hafermilch bei REWE und leg sie in den Warenkorb."
- "Stell einen Timer auf zehn Minuten." / "Weck mich um halb sieben."
- "Öffne Google Präsentationen." / "Nächste Folie."

## Microsoft account

With `MS_CLIENT_ID` set, Casper uses your real Microsoft account through the
Microsoft Graph API. Delegated permissions: `Calendars.ReadWrite`,
`Tasks.ReadWrite`, `Mail.Read`, `Notes.ReadWrite`, `User.Read`.

To Do details: items are found by fuzzy title across all lists (or in one named
list). `update_todo_item` renames, sets or clears the due date, sets importance,
or moves an item to another list (Graph cannot move, so the item is copied
first and the original is deleted afterwards). Mail is strictly read-only, and
Casper treats mail, note and web text as data, never as instructions.

### One-time setup

1. Register an app at <https://entra.microsoft.com> under *App registrations*.
   Choose the account type that matches you (personal accounts need "personal
   Microsoft accounts" enabled).
2. Under *Authentication*, set **Allow public client flows** to **Yes**.
3. Under *API permissions*, add the five delegated Microsoft Graph permissions above.
4. Put the *Application (client) ID* into `.env` as `MS_CLIENT_ID`. For a work
   or school account also set `MS_TENANT_ID`. Leave it empty for personal accounts.
5. Sign in once. The script prints a link and a code in German:

   ```bash
   python auth_microsoft.py
   ```

   Open the link, enter the code, confirm. Tokens are stored in
   `.msal_cache.json` (git-ignored, owner-only permissions where supported).
   `python auth_microsoft.py --logout` deletes them.
6. Restart the backend.

**Upgrading from an earlier version:** OneNote added the `Notes.ReadWrite`
scope. Add it in Azure and run `python auth_microsoft.py --logout` followed by
`python auth_microsoft.py` once so the new permission is granted.

## Gmail (optional)

Gmail is off unless `credentials.json` exists; the server starts normally
without it.

1. In <https://console.cloud.google.com> create a project, enable the **Gmail API**,
   configure the OAuth consent screen, and create an **OAuth client ID of type
   "Desktop app"**.
2. Download the JSON as `credentials.json` into the project folder.
3. Run once: `python auth_google.py` (opens the browser). The token is stored in
   `token_google.json`. `python auth_google.py --logout` deletes it.
4. Restart the backend.

Both files are git-ignored. **Heads-up:** while the consent screen is in
"Testing" status, Google expires the sign-in after 7 days. Publish the app
("In production", the unverified-app warning is fine for private use) or
re-run `auth_google.py` weekly.

Sending is a two-step process enforced in code: Casper prepares the mail, reads
recipient, subject and content back, and asks. Only if you agree in your
**next** message does it send exactly that mail. A mail is never sent in the
same turn it was prepared, and a prepared mail expires after 10 minutes.

## Google apps and slides

`open_google_app` opens Slides, Sheets, Docs, Forms, Drive, Gmail, Calendar, Meet,
Maps, Keep, Photos or Translate in the browser on the backend's computer. It can
create a new document or search Drive by name ("Suche die Präsentation
Kickoff"). `control_presentation` sends keys to the focused window: next,
previous, first, last, start and stop. Click into the presentation once so it has
the focus. It works for Google Slides and PowerPoint slideshows.

## Timers, alarms, briefing

The backend runs a scheduler. When a timer or alarm fires, the client (polling
`GET /alerts`) plays an alarm sound and Casper announces it, so the signal
comes from the client's speakers, later the phone's. `get_daily_briefing`
collects weather, today's appointments, tasks due today, unread mail (Microsoft
and Gmail), today's planned meal and running timers in one step; add news on
request. A failing source is reported instead of breaking the briefing.

## Without a Microsoft app (mock data)

If `MS_CLIENT_ID` is empty, the `microsoft_todo` and `outlook` tools use
`data.json` in the project root, created on first use with sample lists, events
and e-mails. Delete the file to reset it. `data.json` also holds the meal plan,
recipes, REWE cart and alarms. Mock and real Microsoft tools are never active
together.

## Error handling

Casper never answers with a stack trace. Failures become spoken German replies:
a missing key, an unreachable service, unintelligible audio, or a tool problem
("Ich bin noch nicht mit deinem Microsoft-Konto verbunden. ..."). Only if the TTS
service itself is down is there no audio: the server then returns HTTP 503 with a
JSON message, and the client prints it.

## Adding a tool

Create `tools/your_tool.py` with a `BaseTool` subclass and a module-level
factory: `create_tool()` for one tool (return `None` to stay disabled) or
`create_tools()` for a list (return `[]` to stay disabled). The registry finds it
automatically; `brain.py` and `server.py` do not change.

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

A module that fails to import is logged and skipped; the rest still load.

## Security and Phase 2 notes

- The backend binds to `127.0.0.1` by default and has **no authentication**. For
  the phone on the LAN you would run `uvicorn server:app --host 0.0.0.0 --port 8000`.
  **With Microsoft or Google connected, anyone who can reach the backend can read
  your calendar, tasks and mail, and send mail, through Casper.** Add
  authentication and keep it off untrusted networks before exposing it.
- `.msal_cache.json`, `token_google.json` and `credentials.json` grant account
  access. Keep them private (they are git-ignored).
- The PowerPoint, Google-app and slide-control tools act on the machine that
  runs the backend, not on the client device.
- The first sentence after the wake word is sent to Claude, as is the text of
  tool results (mail previews, web snippets, notes). Wake-word audio that does
  not match stays local.
- `edge-tts` and `v6.db.transport.rest` are free public services without a service
  guarantee; DuckDuckGo search can be rate-limited.
- STT and TTS are interfaces: another engine only needs a new `SpeechToText` or
  `SpeechSynthesizer` subclass and a change in `build_pipeline`.

## Verification status

The project was developed in a sandbox without access to the external services,
so these parts were tested against **simulated** responses and have not yet run
against the real thing. Expect small adjustments on first real use:

- Tested with simulated input: the voice-activity detector and the complete
  hands-free flow (fake microphone and server), wake-word matching, all server
  endpoints, conversation memory, timers and alarms, To Do, OneNote, calendar and
  mail (simulated Graph responses), Gmail (simulated Gmail service), trains,
  Wikipedia and web search (simulated responses), the meal plan, recipes and
  REWE cart, and the slide key sequences (simulated keyboard).
- **Not yet verified against the real service:** Microsoft Graph (including the
  OneNote search parameter), Google sign-in and Gmail, the response format of
  `v6.db.transport.rest`, DuckDuckGo and Wikipedia, REWE search results, the
  Whisper model download and how well it recognises "Casper", `edge-tts`, a real
  microphone and speaker (PortAudio), and real key presses with `pynput`.
- Installing `requirements.txt` and `pip check` succeed on Python 3.13.
