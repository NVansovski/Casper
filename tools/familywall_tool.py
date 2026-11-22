"""Meal plan ("Speiseplan") and recipes, stored locally.

FamilyWall has no public API, so this tool keeps the data in ``data.json`` with the
same concepts (planned meals per day, a recipe box). All logic sits behind the two
tool classes, so a future adapter for a real service only has to replace the
``_read``/``_write`` helpers of :class:`FamilyWallStore`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.data_store import JsonDataStore, get_data_store
from tools.text_matching import best_match
from tools.time_utils import clamp_int, weekday_de

SLOTS = ("breakfast", "lunch", "dinner")
SLOT_NAMES_DE = {"breakfast": "Frühstück", "lunch": "Mittagessen", "dinner": "Abendessen"}
TITLE_CUTOFF = 0.6
MAX_PLAN_DAYS = 14

Json = dict[str, Any]


class FamilyWallStore:
    """Thread-safe access to the meal plan and recipe box inside ``data.json``."""

    def __init__(self, store: JsonDataStore) -> None:
        self._store = store

    def meals(self) -> list[Json]:
        return self._store.read(lambda data: [dict(m) for m in data.get("familywall", {}).get("meals", [])])

    def recipes(self) -> list[Json]:
        return self._store.read(lambda data: [dict(r) for r in data.get("familywall", {}).get("recipes", [])])

    def mutate(self, section: str, mutator: Any) -> Any:
        """Run ``mutator(list)`` on ``meals`` or ``recipes`` and persist the result."""

        def write(data: Json) -> Any:
            box = data.setdefault("familywall", {})
            return mutator(box.setdefault(section, []))

        return self._store.write(write)


def _parse_day(value: Any, today: date, *, required: bool = False) -> date | None:
    if not value:
        if required:
            raise ToolError("Für welches Datum?")
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError as exc:
        raise ToolError("Das Datum konnte ich leider nicht lesen.") from exc


def _slot(value: Any, default: str | None = "dinner") -> str | None:
    if not value:
        return default
    text = str(value).lower()
    aliases = {"frühstück": "breakfast", "mittag": "lunch", "mittagessen": "lunch", "abend": "dinner", "abendessen": "dinner"}
    text = aliases.get(text, text)
    if text not in SLOTS:
        raise ToolError("Die Mahlzeit muss Frühstück, Mittagessen oder Abendessen sein.")
    return text


def _describe_meal(meal: Json) -> Json:
    day = date.fromisoformat(meal["date"])
    info: Json = {
        "date": meal["date"],
        "weekday": weekday_de(day),
        "meal": SLOT_NAMES_DE.get(meal.get("slot", "dinner"), "Abendessen"),
        "title": meal.get("title"),
    }
    if meal.get("notes"):
        info["notes"] = meal["notes"]
    return info


class FamilyWallMealsTool(BaseTool):
    name: ClassVar[str] = "familywall_meals"
    description: ClassVar[str] = (
        "Family meal plan (Speiseplan). Actions: 'get_plan' (what is planned from 'date' for 'days' "
        "days, default today), 'add_meal' (date, title, meal slot), 'update_meal' (find a planned "
        "meal by date/slot/title, then change title, date, slot or notes), 'delete_meal'. "
        "Slots: breakfast, lunch, dinner (default dinner)."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get_plan", "add_meal", "update_meal", "delete_meal"]},
            "date": {"type": "string", "description": "Date YYYY-MM-DD."},
            "days": {"type": "integer", "description": "Days to show for get_plan (1-14). Default 1."},
            "slot": {"type": "string", "enum": list(SLOTS), "description": "Meal slot. Default dinner."},
            "title": {"type": "string", "description": "Dish name (add) or dish to find (update/delete)."},
            "notes": {"type": "string", "description": "Optional notes."},
            "new_title": {"type": "string", "description": "update_meal: new dish name."},
            "new_date": {"type": "string", "description": "update_meal: move to this date."},
            "new_slot": {"type": "string", "enum": list(SLOTS), "description": "update_meal: move to this slot."},
        },
        "required": ["action"],
    }

    def __init__(self, store: FamilyWallStore, timezone: tzinfo) -> None:
        self._store = store
        self._timezone = timezone

    def run(self, arguments: Json) -> Json:
        action = str(arguments.get("action") or "")
        today = datetime.now(self._timezone).date()
        if action == "get_plan":
            return self._get_plan(arguments, today)
        if action == "add_meal":
            return self._add(arguments, today)
        if action in ("update_meal", "delete_meal"):
            return self._change(arguments, today, delete=action == "delete_meal")
        raise ToolError("Diese Aktion für den Speiseplan kenne ich nicht.")

    def _get_plan(self, arguments: Json, today: date) -> Json:
        first = _parse_day(arguments.get("date"), today) or today
        days = clamp_int(arguments.get("days"), default=1, low=1, high=MAX_PLAN_DAYS)
        last = first + timedelta(days=days - 1)
        selected = [m for m in self._store.meals() if first.isoformat() <= m["date"] <= last.isoformat()]
        selected.sort(key=lambda m: (m["date"], SLOTS.index(m.get("slot", "dinner"))))
        return {"from": first.isoformat(), "to": last.isoformat(), "count": len(selected), "meals": [_describe_meal(m) for m in selected]}

    def _add(self, arguments: Json, today: date) -> Json:
        day = _parse_day(arguments.get("date"), today, required=True)
        title = str(arguments.get("title") or "").strip()
        if not title:
            raise ToolError("Was soll es geben?")
        slot = _slot(arguments.get("slot")) or "dinner"
        meal = {"id": uuid.uuid4().hex[:8], "date": day.isoformat() if day else "", "slot": slot, "title": title, "notes": str(arguments.get("notes") or "").strip()}

        def add(meals: list[Json]) -> None:
            for existing in meals:
                if existing["date"] == meal["date"] and existing.get("slot") == slot:
                    raise ToolError(f"Für dieses {SLOT_NAMES_DE[slot]} ist schon '{existing['title']}' geplant. Soll ich es ändern?")
            meals.append(meal)

        self._store.mutate("meals", add)
        return {"status": "added", **_describe_meal(meal)}

    def _change(self, arguments: Json, today: date, *, delete: bool) -> Json:
        day = _parse_day(arguments.get("date"), today)
        slot = _slot(arguments.get("slot"), default=None)
        title = str(arguments.get("title") or "").strip()
        new_day = _parse_day(arguments.get("new_date"), today)
        new_slot = _slot(arguments.get("new_slot"), default=None)
        new_title = str(arguments.get("new_title") or "").strip()
        notes = arguments.get("notes")
        if not delete and not (new_day or new_slot or new_title or notes is not None):
            raise ToolError("Was soll ich am geplanten Gericht ändern?")

        def change(meals: list[Json]) -> Json:
            candidates = [
                m for m in meals
                if (day is None or m["date"] == day.isoformat())
                and (slot is None or m.get("slot") == slot)
                and (day is not None or m["date"] >= today.isoformat())
            ]
            if title:
                match = best_match(title, candidates, key=lambda m: str(m.get("title", "")), cutoff=TITLE_CUTOFF)
            elif len(candidates) == 1:
                match = candidates[0]
            elif not candidates:
                match = None
            else:
                raise ToolError("Welches Gericht meinst du? Nenn mir bitte Tag, Mahlzeit oder den Namen.")
            if match is None:
                raise ToolError("Dieses geplante Gericht finde ich nicht.")
            before = _describe_meal(match)
            if delete:
                meals.remove(match)
                return {"status": "deleted", **before}
            target_date = new_day.isoformat() if new_day else match["date"]
            target_slot = new_slot or match.get("slot", "dinner")
            for other in meals:
                if other is not match and other["date"] == target_date and other.get("slot") == target_slot:
                    raise ToolError(f"Dort ist schon '{other['title']}' geplant.")
            match["date"], match["slot"] = target_date, target_slot
            if new_title:
                match["title"] = new_title
            if notes is not None:
                match["notes"] = str(notes).strip()
            return {"status": "updated", **_describe_meal(match)}

        return self._store.mutate("meals", change)  # type: ignore[no-any-return]


class FamilyWallRecipesTool(BaseTool):
    name: ClassVar[str] = "familywall_recipes"
    description: ClassVar[str] = (
        "Family recipe box. Actions: 'list' (all recipe names), 'get' (ingredients and steps of a "
        "recipe), 'add' (title, ingredients, instructions, servings), 'update' (change fields of "
        "a recipe), 'delete'."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "add", "update", "delete"]},
            "title": {"type": "string", "description": "Recipe name (to add, or to find)."},
            "new_title": {"type": "string", "description": "update: new recipe name."},
            "ingredients": {"type": "array", "items": {"type": "string"}, "description": "Ingredient lines."},
            "instructions": {"type": "string", "description": "Preparation steps."},
            "servings": {"type": "integer", "description": "Number of servings."},
        },
        "required": ["action"],
    }

    def __init__(self, store: FamilyWallStore) -> None:
        self._store = store

    def run(self, arguments: Json) -> Json:
        action = str(arguments.get("action") or "")
        if action == "list":
            return {"recipes": sorted(str(r["title"]) for r in self._store.recipes())}
        title = str(arguments.get("title") or "").strip()
        if not title:
            raise ToolError("Wie heißt das Rezept?")
        if action == "add":
            return self._add(title, arguments)
        if action in ("get", "update", "delete"):
            return self._existing(action, title, arguments)
        raise ToolError("Diese Aktion für Rezepte kenne ich nicht.")

    def _add(self, title: str, arguments: Json) -> Json:
        recipe = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "ingredients": [str(i).strip() for i in arguments.get("ingredients") or [] if str(i).strip()],
            "instructions": str(arguments.get("instructions") or "").strip(),
            "servings": clamp_int(arguments.get("servings"), default=4, low=1, high=50),
        }

        def add(recipes: list[Json]) -> None:
            if best_match(title, recipes, key=lambda r: str(r["title"]), cutoff=0.95) is not None:
                raise ToolError(f"Ein Rezept '{title}' gibt es schon. Soll ich es ändern?")
            recipes.append(recipe)

        self._store.mutate("recipes", add)
        return {"status": "added", "title": title, "ingredients": len(recipe["ingredients"])}

    def _existing(self, action: str, title: str, arguments: Json) -> Json:
        def work(recipes: list[Json]) -> Json:
            match = best_match(title, recipes, key=lambda r: str(r["title"]), cutoff=TITLE_CUTOFF)
            if match is None:
                names = ", ".join(str(r["title"]) for r in recipes) or "keine"
                raise ToolError(f"Ein Rezept '{title}' finde ich nicht. Vorhanden: {names}.")
            if action == "get":
                return {key: match[key] for key in ("title", "servings", "ingredients", "instructions")}
            if action == "delete":
                recipes.remove(match)
                return {"status": "deleted", "title": match["title"]}
            changed = False
            if arguments.get("new_title"):
                match["title"], changed = str(arguments["new_title"]).strip(), True
            if arguments.get("ingredients") is not None:
                match["ingredients"], changed = [str(i).strip() for i in arguments["ingredients"] if str(i).strip()], True
            if arguments.get("instructions") is not None:
                match["instructions"], changed = str(arguments["instructions"]).strip(), True
            if arguments.get("servings") is not None:
                match["servings"], changed = clamp_int(arguments["servings"], default=4, low=1, high=50), True
            if not changed:
                raise ToolError("Was soll ich am Rezept ändern?")
            return {"status": "updated", "title": match["title"]}

        return self._store.mutate("recipes", work)  # type: ignore[no-any-return]


def create_tools() -> list[BaseTool]:
    """Factory used by the tool registry."""
    store = FamilyWallStore(get_data_store())
    return [FamilyWallMealsTool(store, ZoneInfo(get_settings().timezone)), FamilyWallRecipesTool(store)]
