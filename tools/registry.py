"""Dynamic tool registry: discovers tool modules and dispatches LLM tool calls.

Adding a tool requires only a new module in the ``tools`` package that exposes a
parameterless ``create_tool()`` (one :class:`BaseTool`, or ``None`` to stay disabled) or
``create_tools()`` (a list of tools) function.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Iterable
from typing import Any

from anthropic.types import ToolParam

from tools.base import BaseTool, ToolError

logger = logging.getLogger(__name__)

TOOLS_PACKAGE = "tools"
FACTORY_NAME = "create_tool"  # returns one tool, or None to stay disabled
MULTI_FACTORY_NAME = "create_tools"  # returns any number of tools (possibly none)


class ToolRegistry:
    """Holds the available tools and executes them on behalf of the LLM."""

    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    @property
    def names(self) -> list[str]:
        """Names of all registered tools."""
        return list(self._tools)

    def register(self, tool: BaseTool) -> None:
        """Add a tool. Raises ``ValueError`` on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool
        logger.info("Registered tool %r", tool.name)

    def anthropic_schemas(self) -> list[ToolParam]:
        """Return all tool definitions for the Anthropic ``tools`` parameter."""
        return [tool.to_anthropic_schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool and always return a dict. Failures become ``{"error": ...}``.

        The LLM receives the error text and can relay it to the user, so a broken
        tool never crashes the pipeline.
        """
        tool = self._tools.get(name)
        if tool is None:
            logger.error("LLM requested unknown tool %r", name)
            return {"error": f"Das Werkzeug '{name}' gibt es nicht."}
        try:
            return tool.run(arguments)
        except ToolError as exc:
            logger.warning("Tool %r reported an error: %s", name, exc)
            return {"error": str(exc)}
        except Exception:
            logger.exception("Tool %r failed unexpectedly", name)
            return {"error": "Dabei ist ein unerwarteter Fehler aufgetreten."}

    @classmethod
    def discover(cls) -> ToolRegistry:
        """Import every module in the ``tools`` package and register its tool.

        Modules without ``create_tool`` (helpers) are skipped. A module that fails
        to import or initialise is logged and skipped so the others still load.
        """
        package = importlib.import_module(TOOLS_PACKAGE)
        registry = cls()
        for module_info in pkgutil.iter_modules(package.__path__):
            if module_info.name.startswith("_"):
                continue
            module_name = f"{TOOLS_PACKAGE}.{module_info.name}"
            try:
                module = importlib.import_module(module_name)
            except Exception:
                logger.exception("Could not import tool module %s; skipping", module_name)
                continue
            try:
                for tool in _tools_from_module(module):
                    registry.register(tool)
            except Exception:
                logger.exception("Could not create tools from %s; skipping", module_name)
        return registry


def _tools_from_module(module: Any) -> list[BaseTool]:
    """Call ``create_tool`` and/or ``create_tools`` of a module, if it defines them."""
    tools: list[BaseTool] = []
    single = getattr(module, FACTORY_NAME, None)
    if single is not None:
        tool = single()
        if tool is not None:
            tools.append(tool)
    many = getattr(module, MULTI_FACTORY_NAME, None)
    if many is not None:
        tools.extend(many())
    return tools
