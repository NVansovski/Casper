"""LLM brain: Anthropic Messages API with native tool use and multi-tool rounds."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, tzinfo
from typing import Any

import anthropic
from anthropic import NOT_GIVEN, AsyncAnthropic
from anthropic.types import MessageParam, ToolResultBlockParam

from pipeline.errors import ReasoningError
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5

WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

SYSTEM_PROMPT_TEMPLATE = """\
Du bist {name}, ein freundlicher, bodenständiger Hausgeist. Du hilfst im Alltag: \
bei Wetter, Einkaufs- und To-do-Listen, Terminen, E-Mails und Präsentationen.

Sprache: Du sprichst ausschließlich Deutsch, auch wenn der Nutzer etwas anderes sagt.

So antwortest du:
- Kurz und auf den Punkt, meist ein bis drei Sätze. Deine Antwort wird vorgelesen.
- Keine Aufzählungszeichen, keine Sternchen, kein Markdown, keine Emojis, keine Links.
- Keine Floskeln und keine langen Einleitungen.
- Zahlen so aussprechen, wie man sie spricht: "vierzehn Uhr dreißig", "drei Grad".
- Nur Fakten nennen, die aus Werkzeugergebnissen oder dem Gespräch stammen. Erfinde keine Daten, Termine oder Wetterwerte.

Werkzeuge:
- Nutze die Werkzeuge, wenn sie für die Anfrage gebraucht werden. Bei mehreren Wünschen in einer Nachricht erledige alle und fasse das Ergebnis knapp zusammen.
- Meldet ein Werkzeug einen Fehler, erkläre ihn kurz und verständlich und schlage eine Alternative vor, wenn es eine gibt.
- Ist eine Anfrage unklar, frage kurz nach, statt zu raten.

Aktuelles Datum und Uhrzeit: {now}.
"""


def format_german_datetime(moment: datetime) -> str:
    """Format a datetime for the German system prompt, e.g. 'Sonntag, 22.11.2026, 14:05 Uhr'."""
    weekday = WEEKDAYS_DE[moment.weekday()]
    return f"{weekday}, {moment:%d.%m.%Y, %H:%M} Uhr"


class CasperBrain:
    """Runs a conversation turn: the LLM may call tools several times before answering."""

    def __init__(
        self,
        client: AsyncAnthropic,
        registry: ToolRegistry,
        *,
        model: str,
        max_tokens: int,
        assistant_name: str,
        timezone: tzinfo,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self._client = client
        self._registry = registry
        self._model = model
        self._max_tokens = max_tokens
        self._assistant_name = assistant_name
        self._timezone = timezone
        self._max_tool_rounds = max_tool_rounds

    @property
    def tool_names(self) -> list[str]:
        return self._registry.names

    async def respond(self, utterance: str) -> str:
        """Return Casper's spoken German reply to a transcribed utterance."""
        messages: list[MessageParam] = [{"role": "user", "content": utterance}]
        tools = self._registry.anthropic_schemas()

        for round_number in range(1, self._max_tool_rounds + 1):
            response = await self._create_message(messages, tools)
            messages.append({"role": "assistant", "content": response.content})

            tool_calls = [block for block in response.content if block.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_calls:
                reply = _collect_text(response.content)
                if not reply:
                    logger.error("Model returned no text in round %d", round_number)
                    raise ReasoningError("Mir ist gerade nichts eingefallen. Frag bitte noch einmal.")
                return reply

            logger.info(
                "Round %d: running %d tool call(s): %s",
                round_number,
                len(tool_calls),
                ", ".join(call.name for call in tool_calls),
            )
            tool_results = await self._run_tools(tool_calls)
            messages.append({"role": "user", "content": tool_results})

        logger.warning("Tool loop exceeded %d rounds", self._max_tool_rounds)
        raise ReasoningError("Das hat bei mir zu lange gedauert. Kannst du es bitte einfacher sagen?")

    async def _create_message(
        self, messages: list[MessageParam], tools: list[Any]
    ) -> anthropic.types.Message:
        try:
            return await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._system_prompt(),
                tools=tools or NOT_GIVEN,
                messages=messages,
            )
        except anthropic.APITimeoutError as exc:
            raise ReasoningError(
                "Das Nachdenken hat zu lange gedauert. Bitte versuch es noch einmal."
            ) from exc
        except anthropic.APIConnectionError as exc:
            logger.error("Anthropic API unreachable: %s", exc)
            raise ReasoningError(
                "Ich kann mein Sprachmodell gerade nicht erreichen. Prüf bitte die Internetverbindung."
            ) from exc
        except anthropic.APIStatusError as exc:
            logger.error("Anthropic API returned HTTP %s: %s", exc.status_code, exc.message)
            raise ReasoningError(
                "Mein Sprachmodell hat einen Fehler gemeldet. Versuch es gleich noch einmal."
            ) from exc

    async def _run_tools(self, tool_calls: list[Any]) -> list[ToolResultBlockParam]:
        outcomes = await asyncio.gather(
            *(
                asyncio.to_thread(self._registry.execute, call.name, dict(call.input))
                for call in tool_calls
            )
        )
        return [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(outcome, ensure_ascii=False),
            }
            for call, outcome in zip(tool_calls, outcomes, strict=True)
        ]

    def _system_prompt(self) -> str:
        now = datetime.now(self._timezone)
        return SYSTEM_PROMPT_TEMPLATE.format(
            name=self._assistant_name,
            now=format_german_datetime(now),
        )


def _collect_text(content: list[Any]) -> str:
    return " ".join(block.text.strip() for block in content if block.type == "text").strip()
