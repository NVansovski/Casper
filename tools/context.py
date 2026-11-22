"""Per-request context for tools: which conversation turn is currently running."""

from __future__ import annotations

from contextvars import ContextVar

_turn_id: ContextVar[int] = ContextVar("casper_turn_id", default=0)


def set_turn_id(turn_id: int) -> None:
    """Called by the brain at the start of every user utterance."""
    _turn_id.set(turn_id)


def get_turn_id() -> int:
    """The id of the utterance being handled. Increases with every utterance."""
    return _turn_id.get()
