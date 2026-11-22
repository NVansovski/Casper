"""Base classes shared by all Casper tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from anthropic.types import ToolParam


class ToolError(Exception):
    """An expected, user-facing tool failure. The message is German and gets read aloud."""


class BaseTool(ABC):
    """Interface every tool implements.

    Subclasses set the class attributes and implement :meth:`run`. The registry
    exposes the tool to the LLM through :meth:`to_anthropic_schema`.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]

    #: Set by :meth:`ToolRegistry.register`. Lets a tool call its siblings (daily briefing).
    registry: Any = None

    @abstractmethod
    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute the tool synchronously and return a JSON-serialisable result.

        Raises:
            ToolError: For expected failures. The message must be German.
        """

    def to_anthropic_schema(self) -> ToolParam:
        """Return the tool definition in the format the Anthropic Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
