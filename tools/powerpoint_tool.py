"""Finds a .pptx presentation by fuzzy filename match and opens it locally."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.text_matching import best_match

logger = logging.getLogger(__name__)

MATCH_CUTOFF = 0.6
PPTX_SUFFIX = ".pptx"
OFFICE_LOCK_PREFIX = "~$"


class PresentationTool(BaseTool):
    """Searches ``PPTX_SEARCH_DIR`` recursively and opens the best match."""

    name: ClassVar[str] = "find_presentation"
    description: ClassVar[str] = (
        "Find a PowerPoint presentation (.pptx) by its name, with fuzzy matching, "
        "and open it in the default application on this computer."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words from the presentation's file name, e.g. 'Quartalsbericht Q3'.",
            },
        },
        "required": ["query"],
    }

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ToolError("Wie heißt die Präsentation, die ich öffnen soll?")

        search_dir = _configured_search_dir()
        candidates = find_pptx_files(search_dir)
        if not candidates:
            raise ToolError("Im Präsentationsordner habe ich keine PowerPoint-Dateien gefunden.")

        match = best_match(query, candidates, key=lambda path: path.stem, cutoff=MATCH_CUTOFF)
        if match is None:
            raise ToolError(f"Ich habe keine Präsentation gefunden, die zu '{query}' passt.")

        logger.info("Opening presentation %s", match)
        open_with_default_app(match)
        return {"status": "opened", "file": match.name}


def _configured_search_dir() -> Path:
    configured = get_settings().pptx_search_dir.strip()
    if not configured:
        raise ToolError(
            "Für Präsentationen ist noch kein Ordner eingerichtet. "
            "Bitte trag PPTX_SEARCH_DIR in der .env-Datei ein."
        )
    search_dir = Path(configured).expanduser()
    if not search_dir.is_dir():
        raise ToolError("Der eingerichtete Präsentationsordner existiert nicht.")
    return search_dir


def find_pptx_files(search_dir: Path) -> list[Path]:
    """Recursively collect .pptx files, ignoring PowerPoint's temporary lock files."""
    found: list[Path] = []
    for root, _dirs, files in os.walk(search_dir):
        for file_name in files:
            if file_name.lower().endswith(PPTX_SUFFIX) and not file_name.startswith(
                OFFICE_LOCK_PREFIX
            ):
                found.append(Path(root) / file_name)
    return sorted(found)


def open_with_default_app(path: Path) -> None:
    """Open a file with the operating system's default application."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]  # Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        logger.error("Could not open %s: %s", path, exc)
        raise ToolError("Ich konnte die Präsentation auf diesem Gerät nicht öffnen.") from exc


def create_tool() -> BaseTool:
    """Factory used by the tool registry."""
    return PresentationTool()
