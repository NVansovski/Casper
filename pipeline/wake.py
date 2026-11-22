"""Wake-word detection on a transcript ("Casper", "Hallo Casper", ...).

Speech recognition may spell the name differently ("Kasper", "Jasper"), so the match
is fuzzy. Only the first few words are inspected, so a name in the middle of a
sentence does not trigger the assistant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from tools.text_matching import normalize

WAKE_WORD = "casper"
GREETINGS = frozenset({"hallo", "hey", "hi", "hei", "ok", "okay", "moin", "servus", "ey"})
MAX_WORDS_BEFORE_NAME = 2
FUZZY_THRESHOLD = 0.8
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SEPARATORS = " \t\r\n,.;:!?-–—"


@dataclass(frozen=True, slots=True)
class WakeResult:
    """``command`` is what follows the wake word ("" when only the name was said)."""

    detected: bool
    command: str


def _is_wake_word(word: str) -> bool:
    normalized = normalize(word)
    if normalized == WAKE_WORD:
        return True
    return 4 <= len(normalized) <= 8 and SequenceMatcher(None, normalized, WAKE_WORD).ratio() >= FUZZY_THRESHOLD


def detect_wake_word(transcript: str) -> WakeResult:
    """Find the wake word at the start of ``transcript`` and split off the command."""
    words = list(_WORD.finditer(transcript))
    for index, word in enumerate(words[: MAX_WORDS_BEFORE_NAME + 1]):
        if _is_wake_word(word.group()):
            before = words[:index]
            if all(normalize(w.group()) in GREETINGS for w in before):
                return WakeResult(True, transcript[word.end():].strip(_SEPARATORS))
            break
        if normalize(word.group()) not in GREETINGS:
            break
    return WakeResult(False, "")
