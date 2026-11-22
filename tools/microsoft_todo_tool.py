"""Mock Microsoft To Do integration: shopping list and to-do list.

The data models and method signatures mirror the Microsoft Graph To Do resources
(``/me/todo/lists`` and ``/me/todo/lists/{id}/tasks``). To go live, implement
:class:`TodoBackend` with a Graph client (authenticated with ``MS_CLIENT_ID`` and
``MS_TENANT_ID``). The tool layer does not change.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.data_store import JsonDataStore, get_data_store
from tools.text_matching import best_match

LIST_ALIASES: dict[str, str] = {"shopping": "Einkaufsliste", "todo": "Aufgaben"}
MATCH_CUTOFF = 0.75


class TaskStatus(StrEnum):
    """Graph ``todoTaskStatus`` values."""

    NOT_STARTED = "notStarted"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class TodoTaskList:
    """Mirrors Graph ``todoTaskList``."""

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TodoTask:
    """Mirrors Graph ``todoTask``."""

    id: str
    title: str
    status: TaskStatus
    created: datetime
    completed: datetime | None

    @property
    def is_completed(self) -> bool:
        return self.status is TaskStatus.COMPLETED


class TodoBackend(ABC):
    """Storage-agnostic To Do operations. Swap the implementation to go live."""

    @abstractmethod
    def list_task_lists(self) -> list[TodoTaskList]:
        """Return all task lists (Graph: ``GET /me/todo/lists``)."""

    @abstractmethod
    def list_tasks(self, list_id: str, *, include_completed: bool = False) -> list[TodoTask]:
        """Return tasks of a list (Graph: ``GET /me/todo/lists/{id}/tasks``)."""

    @abstractmethod
    def create_task(self, list_id: str, title: str) -> TodoTask:
        """Create a task (Graph: ``POST /me/todo/lists/{id}/tasks``)."""

    @abstractmethod
    def update_task_status(self, list_id: str, task_id: str, status: TaskStatus) -> TodoTask:
        """Change a task's status (Graph: ``PATCH /me/todo/lists/{id}/tasks/{taskId}``)."""

    @abstractmethod
    def delete_task(self, list_id: str, task_id: str) -> None:
        """Delete a task (Graph: ``DELETE /me/todo/lists/{id}/tasks/{taskId}``)."""


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _task_from_raw(raw: dict[str, Any]) -> TodoTask:
    created = _parse_datetime(raw["createdDateTime"])
    if created is None:
        raise ToolError("Meine Datendatei enthält eine ungültige Aufgabe.")
    return TodoTask(
        id=str(raw["id"]),
        title=str(raw["title"]),
        status=TaskStatus(raw["status"]),
        created=created,
        completed=_parse_datetime(raw.get("completedDateTime")),
    )


class LocalTodoBackend(TodoBackend):
    """Mock backend that persists To Do data in the shared ``data.json`` file."""

    def __init__(self, store: JsonDataStore) -> None:
        self._store = store

    def list_task_lists(self) -> list[TodoTaskList]:
        def read(data: dict[str, Any]) -> list[TodoTaskList]:
            return [
                TodoTaskList(id=str(raw["id"]), display_name=str(raw["displayName"]))
                for raw in data["todo"]["lists"]
            ]

        return self._store.read(read)

    def list_tasks(self, list_id: str, *, include_completed: bool = False) -> list[TodoTask]:
        def read(data: dict[str, Any]) -> list[TodoTask]:
            raw_tasks = _tasks_bucket(data, list_id, create=False)
            tasks = [_task_from_raw(raw) for raw in raw_tasks]
            return [task for task in tasks if include_completed or not task.is_completed]

        return self._store.read(read)

    def create_task(self, list_id: str, title: str) -> TodoTask:
        now = datetime.now(UTC).replace(microsecond=0)
        raw: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "title": title,
            "status": TaskStatus.NOT_STARTED.value,
            "createdDateTime": now.isoformat(),
            "completedDateTime": None,
        }

        def write(data: dict[str, Any]) -> TodoTask:
            _tasks_bucket(data, list_id, create=True).append(raw)
            return _task_from_raw(raw)

        return self._store.write(write)

    def update_task_status(self, list_id: str, task_id: str, status: TaskStatus) -> TodoTask:
        now = datetime.now(UTC).replace(microsecond=0)

        def write(data: dict[str, Any]) -> TodoTask:
            raw = _find_raw_task(_tasks_bucket(data, list_id, create=False), task_id)
            raw["status"] = status.value
            raw["completedDateTime"] = (
                now.isoformat() if status is TaskStatus.COMPLETED else None
            )
            return _task_from_raw(raw)

        return self._store.write(write)

    def delete_task(self, list_id: str, task_id: str) -> None:
        def write(data: dict[str, Any]) -> None:
            bucket = _tasks_bucket(data, list_id, create=False)
            bucket.remove(_find_raw_task(bucket, task_id))

        self._store.write(write)


def _tasks_bucket(data: dict[str, Any], list_id: str, *, create: bool) -> list[dict[str, Any]]:
    known_ids = {str(raw["id"]) for raw in data["todo"]["lists"]}
    if list_id not in known_ids:
        raise ToolError("Diese Liste kenne ich nicht.")
    tasks: dict[str, list[dict[str, Any]]] = data["todo"].setdefault("tasks", {})
    if create:
        return tasks.setdefault(list_id, [])
    return tasks.get(list_id, [])


def _find_raw_task(bucket: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    for raw in bucket:
        if raw["id"] == task_id:
            return raw
    raise ToolError("Diese Aufgabe finde ich nicht mehr.")


class MicrosoftTodoTool(BaseTool):
    """Voice-facing tool for reading and editing the shopping and to-do lists."""

    name: ClassVar[str] = "microsoft_todo"
    description: ClassVar[str] = (
        "Manage the user's shopping list ('shopping') and general to-do list ('todo'). "
        "Actions: 'list_items' reads open items, 'add_item' adds an item, "
        "'complete_item' ticks off an item by its title, 'delete_item' removes an item."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_items", "add_item", "complete_item", "delete_item"],
            },
            "list": {
                "type": "string",
                "enum": ["shopping", "todo"],
                "description": "'shopping' for groceries and purchases, 'todo' for everything else.",
            },
            "title": {
                "type": "string",
                "description": "Item name. Required for add_item, complete_item and delete_item.",
            },
        },
        "required": ["action", "list"],
    }

    def __init__(self, backend: TodoBackend) -> None:
        self._backend = backend

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", ""))
        display_name = _resolve_list_alias(str(arguments.get("list", "")))
        todo_list = self._find_list(display_name)

        if action == "list_items":
            tasks = self._backend.list_tasks(todo_list.id)
            return {
                "list": display_name,
                "open_items": [task.title for task in tasks],
                "count": len(tasks),
            }

        title = str(arguments.get("title", "")).strip()
        if action == "add_item":
            if not title:
                raise ToolError("Wie soll der Eintrag heißen?")
            task = self._backend.create_task(todo_list.id, title)
            return {"status": "added", "list": display_name, "item": task.title}

        if not title:
            raise ToolError("Welchen Eintrag meinst du genau?")

        if action == "complete_item":
            task = self._find_task(todo_list.id, title, include_completed=False)
            self._backend.update_task_status(todo_list.id, task.id, TaskStatus.COMPLETED)
            return {"status": "completed", "list": display_name, "item": task.title}

        if action == "delete_item":
            task = self._find_task(todo_list.id, title, include_completed=True)
            self._backend.delete_task(todo_list.id, task.id)
            return {"status": "deleted", "list": display_name, "item": task.title}

        raise ToolError("Diese Aktion kenne ich nicht.")

    def _find_list(self, display_name: str) -> TodoTaskList:
        for todo_list in self._backend.list_task_lists():
            if todo_list.display_name == display_name:
                return todo_list
        raise ToolError(f"Die Liste '{display_name}' gibt es bei mir nicht.")

    def _find_task(self, list_id: str, title: str, *, include_completed: bool) -> TodoTask:
        candidates = self._backend.list_tasks(list_id, include_completed=include_completed)
        match = best_match(title, candidates, key=lambda task: task.title, cutoff=MATCH_CUTOFF)
        if match is None:
            raise ToolError(f"Ich finde '{title}' leider nicht auf der Liste.")
        return match


def _resolve_list_alias(alias: str) -> str:
    try:
        return LIST_ALIASES[alias]
    except KeyError as exc:
        raise ToolError("Ich kenne nur die Einkaufsliste und die Aufgabenliste.") from exc


def create_tool() -> BaseTool | None:
    """Factory used by the tool registry. Disabled when the real Microsoft tools are active."""
    if get_settings().graph_enabled:
        return None
    return MicrosoftTodoTool(LocalTodoBackend(get_data_store()))
