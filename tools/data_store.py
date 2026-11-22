"""Thread-safe JSON file store that backs the mock Microsoft services."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

from config import get_settings
from tools.base import ToolError

logger = logging.getLogger(__name__)

T = TypeVar("T")
JsonDict = dict[str, Any]


class JsonDataStore:
    """Reads and writes one JSON document under a process-wide lock.

    Writes go to a temporary file first and are then swapped in with
    ``os.replace``, so a crash never leaves a half-written ``data.json``.
    """

    def __init__(self, path: Path, seed: Callable[[], JsonDict]) -> None:
        self._path = path
        self._seed = seed
        self._lock = threading.RLock()

    def read(self, reader: Callable[[JsonDict], T]) -> T:
        """Run ``reader`` on the current document without persisting changes."""
        with self._lock:
            return reader(self._load())

    def write(self, mutator: Callable[[JsonDict], T]) -> T:
        """Run ``mutator`` on the current document and persist the result.

        If the mutator raises, nothing is saved.
        """
        with self._lock:
            data = self._load()
            result = mutator(data)
            self._save(data)
            return result

    def _load(self) -> JsonDict:
        if not self._path.exists():
            data = self._seed()
            self._save(data)
            logger.info("Created seed data file at %s", self._path)
            return data
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                loaded: JsonDict = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read data file %s: %s", self._path, exc)
            raise ToolError(
                "Meine lokale Datendatei ist beschädigt. Bitte prüfe die Datei data.json."
            ) from exc
        return loaded

    def _save(self, data: JsonDict) -> None:
        temp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self._path)
        except OSError as exc:
            logger.error("Could not write data file %s: %s", self._path, exc)
            raise ToolError("Ich konnte die Daten gerade nicht speichern.") from exc


def _seed_task(title: str, created: datetime) -> JsonDict:
    return {
        "id": uuid.uuid4().hex,
        "title": title,
        "status": "notStarted",
        "createdDateTime": created.isoformat(),
        "completedDateTime": None,
    }


def default_seed_data() -> JsonDict:
    """Initial content for ``data.json``. Delete the file to reset it."""
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "todo": {
            "lists": [
                {"id": "list-shopping", "displayName": "Einkaufsliste", "wellKnownListName": "none"},
                {"id": "list-todo", "displayName": "Aufgaben", "wellKnownListName": "defaultList"},
            ],
            "tasks": {
                "list-shopping": [
                    _seed_task("Milch", now),
                    _seed_task("Brot", now),
                    _seed_task("Eier", now),
                ],
                "list-todo": [_seed_task("Müll rausbringen", now)],
            },
        },
        "outlook": {
            "events": [
                {
                    "id": uuid.uuid4().hex,
                    "subject": "Team-Meeting",
                    "start": (now + timedelta(hours=2)).isoformat(),
                    "end": (now + timedelta(hours=3)).isoformat(),
                    "location": "Büro",
                },
                {
                    "id": uuid.uuid4().hex,
                    "subject": "Zahnarzttermin",
                    "start": (now + timedelta(days=1, hours=1)).isoformat(),
                    "end": (now + timedelta(days=1, hours=2)).isoformat(),
                    "location": None,
                },
            ],
            "messages": [
                {
                    "id": uuid.uuid4().hex,
                    "subject": "Wartungstermin Heizung",
                    "sender": {"name": "Hausverwaltung", "address": "info@example.com"},
                    "receivedDateTime": (now - timedelta(hours=3)).isoformat(),
                    "isRead": False,
                    "bodyPreview": "Der Techniker kommt am Donnerstag zwischen 8 und 12 Uhr.",
                },
                {
                    "id": uuid.uuid4().hex,
                    "subject": "Rechnung November",
                    "sender": {"name": "Stadtwerke", "address": "rechnung@example.com"},
                    "receivedDateTime": (now - timedelta(days=1)).isoformat(),
                    "isRead": False,
                    "bodyPreview": "Ihre Rechnung für den Monat November ist verfügbar.",
                },
            ],
        },
    }


@lru_cache(maxsize=1)
def get_data_store() -> JsonDataStore:
    """Return the shared store used by all mock services."""
    return JsonDataStore(get_settings().data_file, default_seed_data)
