"""Shopping List MCP server tools for managing a shared shopping list."""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

_store: ShoppingListStore | None = None


class ShoppingListStore:
    """JSON-backed storage for a shopping list."""

    def __init__(self, file_path: str) -> None:
        self._path = file_path
        self._data: dict[str, Any] = {"items": []}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._data = json.load(f)
            # Normalize categories on load
            dirty = False
            for item in self._data.get("items", []):
                cat = item.get("category", "")
                normalized = cat.strip().title() if cat else ""
                if normalized != cat:
                    item["category"] = normalized
                    dirty = True
            if dirty:
                self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def add(
        self,
        name: str,
        quantity: float = 1,
        unit: str = "",
        category: str = "",
        added_by: str = "",
    ) -> dict[str, Any]:
        # Normalize category to title case
        category = category.strip().title() if category else ""
        # Dedup by name (case-insensitive) AND unit match
        for item in self._data["items"]:
            if item["name"].lower() == name.lower() and item["unit"] == unit:
                item["quantity"] += quantity
                # Update category if previously empty
                if not item["category"] and category:
                    item["category"] = category
                self._save()
                return item
        item = {
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "category": category,
            "checked": False,
            "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "added_by": added_by,
        }
        self._data["items"].append(item)
        self._save()
        return item

    def remove(self, names: list[str]) -> list[str]:
        lower_names = {n.lower() for n in names}
        removed = []
        remaining = []
        for item in self._data["items"]:
            if item["name"].lower() in lower_names:
                removed.append(item["name"])
            else:
                remaining.append(item)
        self._data["items"] = remaining
        self._save()
        return removed

    def check(self, names: list[str]) -> list[str]:
        lower_names = {n.lower() for n in names}
        checked = []
        for item in self._data["items"]:
            if item["name"].lower() in lower_names:
                item["checked"] = True
                checked.append(item["name"])
        self._save()
        return checked

    def uncheck(self, names: list[str]) -> list[str]:
        lower_names = {n.lower() for n in names}
        unchecked = []
        for item in self._data["items"]:
            if item["name"].lower() in lower_names:
                item["checked"] = False
                unchecked.append(item["name"])
        self._save()
        return unchecked

    def clear(self, checked_only: bool = True) -> int:
        if checked_only:
            before = len(self._data["items"])
            self._data["items"] = [i for i in self._data["items"] if not i["checked"]]
            removed = before - len(self._data["items"])
        else:
            removed = len(self._data["items"])
            self._data["items"] = []
        self._save()
        return removed

    def get_items(self, category: str | None = None) -> list[dict[str, Any]]:
        items = self._data["items"]
        if category is not None:
            items = [i for i in items if i["category"].lower() == category.lower()]
        return items


def _get_store() -> ShoppingListStore:
    global _store
    if _store is None:
        path = os.environ.get("SHOPPING_LIST_FILE", "data/shopping_list.json")
        _store = ShoppingListStore(path)
    return _store


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool(
    "shopping_list_add",
    "Add one or more items to the shopping list. Deduplicates by name and unit, summing quantities. Always split quantity from unit (e.g. '450g' -> quantity=450 unit='g', '2 cups' -> quantity=2 unit='cup'). Use consistent category names: Produce, Meat, Dairy, Bakery, Frozen, Pantry, Condiments, Spices, Snacks, Beverages, Refrigerated, Other.",
    {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Item name"},
                        "quantity": {"type": "number", "description": "Numeric quantity (default 1). Split from unit: '450g' -> quantity=450, '0.5 cups' -> quantity=0.5, '1/3 cup' -> quantity=0.33"},
                        "unit": {"type": "string", "description": "Unit of measure (e.g. 'g', 'lb', 'cup', 'oz'). Must be separate from quantity."},
                        "category": {"type": "string", "description": "Category for grouping"},
                    },
                    "required": ["name"],
                },
                "description": "Items to add",
            },
            "added_by": {"type": "string", "description": "Slack user ID of who added the items"},
        },
        "required": ["items"],
    },
)
async def shopping_list_add(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        added_by = args.get("added_by", "")
        added = []
        for item_data in args["items"]:
            item = store.add(
                name=item_data["name"],
                quantity=item_data.get("quantity", 1),
                unit=item_data.get("unit", ""),
                category=item_data.get("category", ""),
                added_by=added_by,
            )
            added.append(item["name"])
        return _text(f"Added/updated {len(added)} item(s): {', '.join(added)}")
    except Exception as e:
        return _error(f"Failed to add items: {e}")


@tool(
    "shopping_list_view",
    "View the current shopping list, optionally filtered by category.",
    {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Filter by category (case-insensitive)"},
        },
    },
)
async def shopping_list_view(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        items = store.get_items(category=args.get("category"))
        return _text(json.dumps({"items": items}, indent=2))
    except Exception as e:
        return _error(f"Failed to view shopping list: {e}")


@tool(
    "shopping_list_remove",
    "Remove items from the shopping list by name.",
    {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Item names to remove",
            },
        },
        "required": ["names"],
    },
)
async def shopping_list_remove(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        removed = store.remove(args["names"])
        return _text(f"Removed {len(removed)} item(s): {', '.join(removed) if removed else 'none found'}")
    except Exception as e:
        return _error(f"Failed to remove items: {e}")


@tool(
    "shopping_list_check",
    "Mark items as checked/purchased on the shopping list.",
    {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Item names to check off",
            },
        },
        "required": ["names"],
    },
)
async def shopping_list_check(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        checked = store.check(args["names"])
        return _text(f"Checked {len(checked)} item(s): {', '.join(checked) if checked else 'none found'}")
    except Exception as e:
        return _error(f"Failed to check items: {e}")


@tool(
    "shopping_list_uncheck",
    "Uncheck items on the shopping list.",
    {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Item names to uncheck",
            },
        },
        "required": ["names"],
    },
)
async def shopping_list_uncheck(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        unchecked = store.uncheck(args["names"])
        return _text(f"Unchecked {len(unchecked)} item(s): {', '.join(unchecked) if unchecked else 'none found'}")
    except Exception as e:
        return _error(f"Failed to uncheck items: {e}")


@tool(
    "shopping_list_clear",
    "Clear items from the shopping list. By default clears only checked items.",
    {
        "type": "object",
        "properties": {
            "all": {"type": "boolean", "description": "If true, clear all items. Otherwise only checked items."},
            "checked_only": {"type": "boolean", "description": "If true (default), only clear checked items."},
        },
    },
)
async def shopping_list_clear(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        # "all" flag overrides checked_only
        if args.get("all", False):
            checked_only = False
        else:
            checked_only = args.get("checked_only", True)
        removed = store.clear(checked_only=checked_only)
        return _text(f"Cleared {removed} item(s) from the shopping list.")
    except Exception as e:
        return _error(f"Failed to clear shopping list: {e}")


def _item_label(item: dict[str, Any]) -> str:
    """Build a display label for a shopping list item."""
    name = item["name"]
    quantity = item.get("quantity", 1)
    unit = item.get("unit", "")
    if unit:
        return f"{name} ({quantity} {unit})"
    if quantity != 1:
        return f"{name} ({quantity})"
    return name


def build_shopping_list_blocks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build Slack Block Kit blocks grouped by category with checkboxes."""
    if not items:
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Your shopping list is empty."},
            }
        ]

    # Group items by category
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        cat = item.get("category", "") or "Other"
        grouped.setdefault(cat, []).append(item)

    blocks: list[dict[str, Any]] = []
    for category, cat_items in grouped.items():
        # Category header
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": category},
        })

        # Build checkbox options and initial_options for this category
        options = []
        initial_options = []
        for item in cat_items:
            label = _item_label(item)
            option = {
                "text": {"type": "plain_text", "text": label},
                "value": item["name"],
            }
            options.append(option)
            if item.get("checked", False):
                initial_options.append(option)

        checkbox_block: dict[str, Any] = {
            "type": "actions",
            "elements": [
                {
                    "type": "checkboxes",
                    "action_id": f"shopping_list_check_item_{category}",
                    "options": options,
                }
            ],
        }
        if initial_options:
            checkbox_block["elements"][0]["initial_options"] = initial_options

        blocks.append(checkbox_block)

    return blocks


import re


# ---------------------------------------------------------------------------
# Recipe storage
# ---------------------------------------------------------------------------

_recipe_store: RecipeStore | None = None


class RecipeStore:
    """JSON-backed storage for saved recipes."""

    def __init__(self, file_path: str) -> None:
        self._path = file_path
        self._data: dict[str, Any] = {"recipes": []}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._data = json.load(f)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    @staticmethod
    def _slugify(name: str) -> str:
        slug = name.lower().replace(" ", "-")
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        return slug

    def save(
        self,
        name: str,
        source_url: str,
        ingredients: list[dict[str, Any]],
        instructions: str,
        added_by: str,
    ) -> dict[str, Any]:
        recipe_id = self._slugify(name)
        recipe = {
            "id": recipe_id,
            "name": name,
            "source_url": source_url,
            "ingredients": ingredients,
            "instructions": instructions,
            "added_by": added_by,
            "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        # Overwrite if same id exists
        self._data["recipes"] = [
            r for r in self._data["recipes"] if r["id"] != recipe_id
        ]
        self._data["recipes"].append(recipe)
        self._save()
        return recipe

    def list_recipes(self) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "name": r["name"], "source_url": r["source_url"], "added_at": r["added_at"]}
            for r in self._data["recipes"]
        ]

    def get_recipe(self, id_or_name: str) -> dict[str, Any] | None:
        # Exact id match first
        for r in self._data["recipes"]:
            if r["id"] == id_or_name:
                return r
        # Case-insensitive name substring
        query_lower = id_or_name.lower()
        for r in self._data["recipes"]:
            if query_lower in r["name"].lower():
                return r
        return None

    def delete(self, id_or_name: str) -> bool:
        recipe = self.get_recipe(id_or_name)
        if recipe is None:
            return False
        self._data["recipes"] = [
            r for r in self._data["recipes"] if r["id"] != recipe["id"]
        ]
        self._save()
        return True


def _get_recipe_store() -> RecipeStore:
    global _recipe_store
    if _recipe_store is None:
        path = os.environ.get("RECIPE_FILE", "data/recipes.json")
        _recipe_store = RecipeStore(path)
    return _recipe_store


@tool(
    "recipe_save",
    "When adding items from a recipe URL, always save the recipe first so it can be recalled later for cooking.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Recipe name"},
            "source_url": {"type": "string", "description": "URL the recipe was found at"},
            "ingredients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "string"},
                        "unit": {"type": "string"},
                    },
                },
                "description": "List of ingredients",
            },
            "instructions": {"type": "string", "description": "Cooking instructions"},
            "added_by": {"type": "string", "description": "Slack user ID"},
        },
        "required": ["name", "source_url", "ingredients", "instructions", "added_by"],
    },
)
async def recipe_save(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_recipe_store()
        recipe = store.save(
            name=args["name"],
            source_url=args["source_url"],
            ingredients=args["ingredients"],
            instructions=args["instructions"],
            added_by=args["added_by"],
        )
        return _text(f"Saved recipe: {recipe['name']} (id: {recipe['id']})")
    except Exception as e:
        return _error(f"Failed to save recipe: {e}")


@tool(
    "recipe_list",
    "List all saved recipes.",
    {
        "type": "object",
        "properties": {},
    },
)
async def recipe_list(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_recipe_store()
        recipes = store.list_recipes()
        if not recipes:
            return _text("No saved recipes.")
        lines = [f"- {r['name']} (id: {r['id']})" for r in recipes]
        return _text("Saved recipes:\n" + "\n".join(lines))
    except Exception as e:
        return _error(f"Failed to list recipes: {e}")


@tool(
    "recipe_view",
    "View a saved recipe by id or name substring.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Recipe id or name substring to search for"},
        },
        "required": ["query"],
    },
)
async def recipe_view(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_recipe_store()
        recipe = store.get_recipe(args["query"])
        if recipe is None:
            return _text(f"Recipe not found: {args['query']}")
        return _text(json.dumps(recipe, indent=2))
    except Exception as e:
        return _error(f"Failed to view recipe: {e}")


@tool(
    "recipe_delete",
    "Delete a saved recipe by id or name substring.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Recipe id or name substring to delete"},
        },
        "required": ["query"],
    },
)
async def recipe_delete(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_recipe_store()
        if store.delete(args["query"]):
            return _text(f"Deleted recipe matching: {args['query']}")
        return _text(f"Recipe not found: {args['query']}")
    except Exception as e:
        return _error(f"Failed to delete recipe: {e}")


SHOPPING_LIST_TOOLS: list[SdkMcpTool] = [
    shopping_list_add,
    shopping_list_view,
    shopping_list_remove,
    shopping_list_check,
    shopping_list_uncheck,
    shopping_list_clear,
    recipe_save,
    recipe_list,
    recipe_view,
    recipe_delete,
]
