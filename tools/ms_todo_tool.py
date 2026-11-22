"""Microsoft Graph To Do tools: read lists and add items."""

from __future__ import annotations

from typing import Any, ClassVar

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.graph_client import GraphClient, get_graph_client
from tools.text_matching import best_match

LIST_MATCH_CUTOFF = 0.6
MAX_ITEMS = 50


def _fetch_lists(client: GraphClient) -> list[dict[str, Any]]:
    payload = client.get("/me/todo/lists", params={"$top": "50"})
    return [raw for raw in payload.get("value", []) if raw.get("id")]


def _resolve_list(client: GraphClient, list_name: str) -> dict[str, Any]:
    """Find a list by fuzzy name, or raise a German error that names the available lists."""
    lists = _fetch_lists(client)
    match = best_match(
        list_name, lists, key=lambda raw: str(raw.get("displayName", "")), cutoff=LIST_MATCH_CUTOFF
    )
    if match is not None:
        return match
    names = ", ".join(str(raw.get("displayName", "")) for raw in lists) or "keine"
    raise ToolError(f"Eine Liste namens '{list_name}' gibt es nicht. Vorhandene Listen: {names}.")


class GetTodoListsTool(BaseTool):
    """``GET /me/todo/lists`` and, optionally, the open tasks of one list."""

    name: ClassVar[str] = "get_todo_lists"
    description: ClassVar[str] = (
        "List the user's Microsoft To Do lists. Pass 'list_name' (for example 'Einkaufsliste') "
        "to also read the open items of that list."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Name of one list whose open items should be returned. Optional.",
            },
        },
        "required": [],
    }

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        list_name = str(arguments.get("list_name") or "").strip()
        if not list_name:
            lists = _fetch_lists(self._client)
            return {"lists": [str(raw.get("displayName", "")) for raw in lists]}

        todo_list = _resolve_list(self._client, list_name)
        payload = self._client.get(
            f"/me/todo/lists/{todo_list['id']}/tasks",
            params={"$filter": "status ne 'completed'", "$top": str(MAX_ITEMS)},
        )
        items = [str(raw.get("title", "")) for raw in payload.get("value", [])]
        return {"list": todo_list.get("displayName"), "open_items": items, "count": len(items)}


class AddTodoItemTool(BaseTool):
    """``POST /me/todo/lists/{id}/tasks``: add an item to a list."""

    name: ClassVar[str] = "add_todo_item"
    description: ClassVar[str] = (
        "Add an item to one of the user's Microsoft To Do lists, for example 'Milch' to the "
        "'Einkaufsliste'. Use the shopping list for groceries."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "Name of the list, e.g. 'Einkaufsliste'."},
            "title": {"type": "string", "description": "The item to add."},
        },
        "required": ["list_name", "title"],
    }

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        list_name = str(arguments.get("list_name") or "").strip()
        title = str(arguments.get("title") or "").strip()
        if not list_name:
            raise ToolError("In welche Liste soll ich das eintragen?")
        if not title:
            raise ToolError("Wie soll der Eintrag heißen?")

        todo_list = _resolve_list(self._client, list_name)
        self._client.post(f"/me/todo/lists/{todo_list['id']}/tasks", {"title": title})
        return {"status": "added", "list": todo_list.get("displayName"), "item": title}


def create_tools() -> list[BaseTool]:
    """Factory used by the tool registry. Active only when Microsoft is configured."""
    if not get_settings().graph_enabled:
        return []
    client = get_graph_client()
    return [GetTodoListsTool(client), AddTodoItemTool(client)]
