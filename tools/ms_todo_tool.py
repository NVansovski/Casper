"""Microsoft Graph To Do tools: lists, items, and the "Mein Tag" / "Wichtig" views."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, tzinfo
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.graph_client import GraphClient, get_graph_client
from tools.text_matching import best_match

LIST_MATCH_CUTOFF = 0.6
TASK_MATCH_CUTOFF = 0.75
MAX_TASKS_PER_LIST = 100
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Json = dict[str, Any]


# ---------------------------------------------------------------- shared helpers
def _fetch_lists(client: GraphClient) -> list[Json]:
    payload = client.get("/me/todo/lists", params={"$top": "50"})
    return [raw for raw in payload.get("value", []) if raw.get("id")]


def _resolve_list(client: GraphClient, list_name: str) -> Json:
    """Find a list by fuzzy name, or raise a German error that names the available lists."""
    lists = _fetch_lists(client)
    match = best_match(
        list_name, lists, key=lambda raw: str(raw.get("displayName", "")), cutoff=LIST_MATCH_CUTOFF
    )
    if match is not None:
        return match
    names = ", ".join(str(raw.get("displayName", "")) for raw in lists) or "keine"
    raise ToolError(f"Eine Liste namens '{list_name}' gibt es nicht. Vorhandene Listen: {names}.")


def _open_tasks(client: GraphClient, list_id: str, extra_filter: str | None = None) -> list[Json]:
    flt = "status ne 'completed'" + (f" and {extra_filter}" if extra_filter else "")
    payload = client.get(
        f"/me/todo/lists/{list_id}/tasks", params={"$filter": flt, "$top": str(MAX_TASKS_PER_LIST)}
    )
    return list(payload.get("value", []))


def _open_tasks_everywhere(
    client: GraphClient, extra_filter: str | None = None
) -> list[tuple[Json, Json]]:
    """Open tasks of all lists as ``(list, task)`` pairs. Lists are queried in parallel."""
    lists = _fetch_lists(client)
    if not lists:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(lists))) as pool:
        per_list = list(pool.map(lambda lst: _open_tasks(client, str(lst["id"]), extra_filter), lists))
    return [(lst, task) for lst, tasks in zip(lists, per_list, strict=True) for task in tasks]


def _due_date(task: Json) -> str | None:
    """Due date as YYYY-MM-DD. To Do stores a date at midnight, so only the date part counts."""
    due = (task.get("dueDateTime") or {}).get("dateTime")
    return str(due)[:10] if due else None


def _describe(todo_list: Json, task: Json, today: str | None = None) -> Json:
    due = _due_date(task)
    info: Json = {
        "title": task.get("title", ""),
        "list": todo_list.get("displayName"),
        "important": task.get("importance") == "high",
        "due": due,
    }
    if today and due and due < today:
        info["overdue"] = True
    return info


def _find_task(client: GraphClient, title: str, list_name: str | None) -> tuple[Json, Json]:
    """Find an open task by fuzzy title, in one named list or across all lists."""
    if list_name:
        todo_list = _resolve_list(client, list_name)
        pairs = [(todo_list, task) for task in _open_tasks(client, str(todo_list["id"]))]
    else:
        pairs = _open_tasks_everywhere(client)
    match = best_match(
        title, pairs, key=lambda pair: str(pair[1].get("title", "")), cutoff=TASK_MATCH_CUTOFF
    )
    if match is None:
        raise ToolError(f"Ich finde '{title}' leider nicht in deinen offenen Aufgaben.")
    return match


def _due_payload(date_text: str) -> Json:
    if not _DATE_RE.match(date_text):
        raise ToolError("Das Fälligkeitsdatum konnte ich nicht lesen.")
    return {"dateTime": f"{date_text}T00:00:00", "timeZone": "UTC"}


def _require(arguments: Json, key: str, question: str) -> str:
    value = str(arguments.get(key) or "").strip()
    if not value:
        raise ToolError(question)
    return value


class _TodoTool(BaseTool):
    """Common constructor for tools that talk to Graph."""

    def __init__(self, client: GraphClient, timezone: tzinfo) -> None:
        self._client = client
        self._timezone = timezone


# ---------------------------------------------------------------- tools
class GetTodoListsTool(_TodoTool):
    name: ClassVar[str] = "get_todo_lists"
    description: ClassVar[str] = (
        "List the user's Microsoft To Do lists. Pass 'list_name' (for example 'Einkaufsliste') "
        "to also read the open items of that list."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "List whose open items to return. Optional."},
        },
        "required": [],
    }

    def run(self, arguments: Json) -> Json:
        list_name = str(arguments.get("list_name") or "").strip()
        if not list_name:
            return {"lists": [str(raw.get("displayName", "")) for raw in _fetch_lists(self._client)]}
        todo_list = _resolve_list(self._client, list_name)
        tasks = _open_tasks(self._client, str(todo_list["id"]))
        today = datetime.now(self._timezone).date().isoformat()
        return {
            "list": todo_list.get("displayName"),
            "count": len(tasks),
            "open_items": [_describe(todo_list, task, today) for task in tasks],
        }


class AddTodoItemTool(_TodoTool):
    name: ClassVar[str] = "add_todo_item"
    description: ClassVar[str] = (
        "Add an item to one of the user's Microsoft To Do lists, for example 'Milch' to the "
        "'Einkaufsliste'. Optionally set a due date (this puts it into 'Mein Tag' when due today) "
        "and mark it important."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "Name of the list, e.g. 'Einkaufsliste'."},
            "title": {"type": "string", "description": "The item to add."},
            "due_date": {"type": "string", "description": "Due date as YYYY-MM-DD. Optional."},
            "important": {"type": "boolean", "description": "Mark as important. Optional."},
        },
        "required": ["list_name", "title"],
    }

    def run(self, arguments: Json) -> Json:
        list_name = _require(arguments, "list_name", "In welche Liste soll ich das eintragen?")
        title = _require(arguments, "title", "Wie soll der Eintrag heißen?")
        body: Json = {"title": title}
        if arguments.get("due_date"):
            body["dueDateTime"] = _due_payload(str(arguments["due_date"]))
        if arguments.get("important"):
            body["importance"] = "high"
        todo_list = _resolve_list(self._client, list_name)
        self._client.post(f"/me/todo/lists/{todo_list['id']}/tasks", body)
        return {
            "status": "added",
            "list": todo_list.get("displayName"),
            "item": title,
            "due": arguments.get("due_date"),
            "important": bool(arguments.get("important")),
        }


class CompleteTodoItemTool(_TodoTool):
    name: ClassVar[str] = "complete_todo_item"
    description: ClassVar[str] = (
        "Mark an open To Do item as done (abhaken). The item is found by its title in all lists, "
        "or only in 'list_name' if given."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Title of the item to tick off."},
            "list_name": {"type": "string", "description": "Restrict the search to this list. Optional."},
        },
        "required": ["title"],
    }

    def run(self, arguments: Json) -> Json:
        title = _require(arguments, "title", "Welchen Eintrag soll ich abhaken?")
        todo_list, task = _find_task(self._client, title, arguments.get("list_name"))
        self._client.patch(
            f"/me/todo/lists/{todo_list['id']}/tasks/{task['id']}", {"status": "completed"}
        )
        return {"status": "completed", "item": task.get("title"), "list": todo_list.get("displayName")}


class UpdateTodoItemTool(_TodoTool):
    name: ClassVar[str] = "update_todo_item"
    description: ClassVar[str] = (
        "Change an open To Do item: rename it ('new_title'), set or clear the due date "
        "('due_date' as YYYY-MM-DD, or 'none' to clear), set importance, or move it to another "
        "list ('move_to_list'). The item is found by its title."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Current title of the item."},
            "list_name": {"type": "string", "description": "List the item is in. Optional."},
            "new_title": {"type": "string", "description": "New title. Optional."},
            "due_date": {"type": "string", "description": "New due date YYYY-MM-DD, or 'none'. Optional."},
            "important": {"type": "boolean", "description": "Set or clear importance. Optional."},
            "move_to_list": {"type": "string", "description": "Move the item to this list. Optional."},
        },
        "required": ["title"],
    }

    def run(self, arguments: Json) -> Json:
        title = _require(arguments, "title", "Welchen Eintrag soll ich ändern?")
        todo_list, task = _find_task(self._client, title, arguments.get("list_name"))

        changes: Json = {}
        if arguments.get("new_title"):
            changes["title"] = str(arguments["new_title"]).strip()
        if arguments.get("due_date"):
            due = str(arguments["due_date"]).strip()
            changes["dueDateTime"] = None if due.lower() in {"none", "keine", "null"} else _due_payload(due)
        if arguments.get("important") is not None:
            changes["importance"] = "high" if arguments["important"] else "normal"
        target_name = str(arguments.get("move_to_list") or "").strip()
        if not changes and not target_name:
            raise ToolError("Was soll ich an dem Eintrag ändern?")

        target = _resolve_list(self._client, target_name) if target_name else None
        moved = target is not None and str(target["id"]) != str(todo_list["id"])
        if changes and not moved:
            self._client.patch(f"/me/todo/lists/{todo_list['id']}/tasks/{task['id']}", changes)
        if moved and target is not None:
            # Graph cannot move a task: create the copy first, delete the original only afterwards.
            copy: Json = {
                key: task[key]
                for key in ("title", "importance", "dueDateTime", "body", "reminderDateTime", "isReminderOn")
                if task.get(key) is not None
            }
            copy.update({key: value for key, value in changes.items() if value is not None})
            if changes.get("dueDateTime", 0) is None:
                copy.pop("dueDateTime", None)
            self._client.post(f"/me/todo/lists/{target['id']}/tasks", copy)
            self._client.delete(f"/me/todo/lists/{todo_list['id']}/tasks/{task['id']}")
        return {
            "status": "moved" if moved else "updated",
            "item": changes.get("title") or task.get("title"),
            "list": (target or todo_list).get("displayName"),
        }


class DeleteTodoItemTool(_TodoTool):
    name: ClassVar[str] = "delete_todo_item"
    description: ClassVar[str] = (
        "Delete a To Do item permanently. The item is found by its title in all lists, or only "
        "in 'list_name' if given."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Title of the item to delete."},
            "list_name": {"type": "string", "description": "Restrict the search to this list. Optional."},
        },
        "required": ["title"],
    }

    def run(self, arguments: Json) -> Json:
        title = _require(arguments, "title", "Welchen Eintrag soll ich löschen?")
        todo_list, task = _find_task(self._client, title, arguments.get("list_name"))
        self._client.delete(f"/me/todo/lists/{todo_list['id']}/tasks/{task['id']}")
        return {"status": "deleted", "item": task.get("title"), "list": todo_list.get("displayName")}


class GetTodoViewTool(_TodoTool):
    name: ClassVar[str] = "get_todo_view"
    description: ClassVar[str] = (
        "Read a smart view across ALL To Do lists. 'my_day' = open tasks due today or overdue "
        "('Mein Tag', 'Was steht heute an?'). 'important' = open tasks marked important "
        "('Wichtig', 'Was ist wichtig?')."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {"view": {"type": "string", "enum": ["my_day", "important"]}},
        "required": ["view"],
    }

    def run(self, arguments: Json) -> Json:
        view = str(arguments.get("view", ""))
        today = datetime.now(self._timezone).date().isoformat()
        if view == "my_day":
            pairs = _open_tasks_everywhere(self._client)
            pairs = [pair for pair in pairs if (due := _due_date(pair[1])) and due <= today]
            label = "Mein Tag"
        elif view == "important":
            pairs = _open_tasks_everywhere(self._client, "importance eq 'high'")
            label = "Wichtig"
        else:
            raise ToolError("Diese Ansicht kenne ich nicht. Ich kenne 'Mein Tag' und 'Wichtig'.")
        pairs.sort(key=lambda pair: (_due_date(pair[1]) or "9999-12-31", str(pair[1].get("title", ""))))
        return {
            "view": label,
            "today": today,
            "count": len(pairs),
            "tasks": [_describe(todo_list, task, today) for todo_list, task in pairs],
        }


def create_tools() -> list[BaseTool]:
    """Factory used by the tool registry. Active only when Microsoft is configured."""
    settings = get_settings()
    if not settings.graph_enabled:
        return []
    client = get_graph_client()
    timezone = ZoneInfo(settings.timezone)
    return [
        GetTodoListsTool(client, timezone),
        AddTodoItemTool(client, timezone),
        CompleteTodoItemTool(client, timezone),
        UpdateTodoItemTool(client, timezone),
        DeleteTodoItemTool(client, timezone),
        GetTodoViewTool(client, timezone),
    ]
