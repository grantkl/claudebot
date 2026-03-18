"""Tests for the Shopping List MCP server tools."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# Build a mock claude_agent_sdk with a working @tool decorator
def _make_sdk_mock() -> MagicMock:
    sdk = MagicMock()
    sdk.SdkMcpTool = MagicMock

    def _tool(name: str, description: str, schema: Any) -> Any:
        def decorator(fn: Any) -> Any:
            wrapper = MagicMock()
            wrapper.handler = fn
            wrapper.__name__ = fn.__name__
            return wrapper
        return decorator

    sdk.tool = _tool
    return sdk


# Force our mock with a working @tool decorator
sys.modules["claude_agent_sdk"] = _make_sdk_mock()
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())

import importlib  # noqa: E402
sys.modules.pop("src.mcp.shopping_list_server", None)

from src.mcp import shopping_list_server  # noqa: E402

importlib.reload(shopping_list_server)

# Access underlying async handlers
_add = shopping_list_server.shopping_list_add.handler
_view = shopping_list_server.shopping_list_view.handler
_remove = shopping_list_server.shopping_list_remove.handler
_check = shopping_list_server.shopping_list_check.handler
_uncheck = shopping_list_server.shopping_list_uncheck.handler
_clear = shopping_list_server.shopping_list_clear.handler
_lists = shopping_list_server.shopping_list_lists.handler
_delete_list = shopping_list_server.shopping_list_delete_list.handler


def _parse_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


@pytest.fixture(autouse=True)
def _reset_store():
    shopping_list_server._store = None
    yield
    shopping_list_server._store = None


# ---------------------------------------------------------------------------
# TestShoppingListStore
# ---------------------------------------------------------------------------
class TestShoppingListStore:
    def test_load_empty_file(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        assert store.get_items() == []

    def test_load_missing_file(self, tmp_path):
        f = str(tmp_path / "nonexistent" / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        assert store.get_items() == []

    def test_save_and_reload(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs", quantity=6, unit="", category="Dairy")
        # Reload from disk
        store2 = shopping_list_server.ShoppingListStore(f)
        items = store2.get_items()
        assert len(items) == 1
        assert items[0]["name"] == "Eggs"
        assert items[0]["quantity"] == 6

    def test_add_item_defaults(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk")
        items = store.get_items()
        assert len(items) == 1
        item = items[0]
        assert item["name"] == "Milk"
        assert item["quantity"] == 1
        assert item["unit"] == ""
        assert item["category"] == ""
        assert item["checked"] is False
        assert "added_at" in item
        assert item["added_by"] == ""

    def test_add_item_with_all_fields(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Chicken Breast", quantity=2, unit="lbs", category="Meat", added_by="U123")
        items = store.get_items()
        assert len(items) == 1
        item = items[0]
        assert item["name"] == "Chicken Breast"
        assert item["quantity"] == 2
        assert item["unit"] == "lbs"
        assert item["category"] == "Meat"
        assert item["added_by"] == "U123"

    def test_dedup_same_name_same_unit_sums_quantities(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs", quantity=1)
        store.add("eggs", quantity=3)  # case-insensitive match, same unit (empty)
        items = store.get_items()
        assert len(items) == 1
        assert items[0]["quantity"] == 4

    def test_dedup_case_insensitive(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("MILK", quantity=1, unit="gallon")
        store.add("milk", quantity=2, unit="gallon")
        items = store.get_items()
        assert len(items) == 1
        assert items[0]["quantity"] == 3

    def test_different_units_stay_separate(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", quantity=1, unit="cup")
        store.add("Milk", quantity=2, unit="tbsp")
        items = store.get_items()
        assert len(items) == 2

    def test_remove_by_name_case_insensitive(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs")
        store.add("Milk")
        store.remove(["eggs"])
        items = store.get_items()
        assert len(items) == 1
        assert items[0]["name"] == "Milk"

    def test_remove_multiple(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs")
        store.add("Milk")
        store.add("Bread")
        store.remove(["Eggs", "Bread"])
        items = store.get_items()
        assert len(items) == 1
        assert items[0]["name"] == "Milk"

    def test_check_items(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs")
        store.add("Milk")
        store.check(["eggs"])
        items = store.get_items()
        eggs = [i for i in items if i["name"] == "Eggs"][0]
        milk = [i for i in items if i["name"] == "Milk"][0]
        assert eggs["checked"] is True
        assert milk["checked"] is False

    def test_uncheck_items(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs")
        store.check(["Eggs"])
        store.uncheck(["eggs"])
        items = store.get_items()
        assert items[0]["checked"] is False

    def test_clear_checked_only(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs")
        store.add("Milk")
        store.check(["Eggs"])
        store.clear(checked_only=True)
        items = store.get_items()
        assert len(items) == 1
        assert items[0]["name"] == "Milk"

    def test_clear_all(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs")
        store.add("Milk")
        store.check(["Eggs"])
        store.clear(checked_only=False)
        items = store.get_items()
        assert len(items) == 0

    def test_get_items_filter_by_category(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs", category="Dairy")
        store.add("Bread", category="Bakery")
        store.add("Milk", category="Dairy")
        dairy = store.get_items(category="Dairy")
        assert len(dairy) == 2
        names = {i["name"] for i in dairy}
        assert names == {"Eggs", "Milk"}

    def test_get_items_all(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Eggs", category="Dairy")
        store.add("Bread", category="Bakery")
        all_items = store.get_items()
        assert len(all_items) == 2


# ---------------------------------------------------------------------------
# TestMultipleListsStore
# ---------------------------------------------------------------------------
class TestMultipleListsStore:
    """Tests for multi-list functionality at the store level."""

    def test_add_to_different_lists(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill bits", list_name="hardware")
        assert len(store.get_items(list_name="grocery")) == 1
        assert len(store.get_items(list_name="hardware")) == 1
        assert store.get_items(list_name="grocery")[0]["name"] == "Milk"
        assert store.get_items(list_name="hardware")[0]["name"] == "Drill bits"

    def test_default_list_is_grocery(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk")
        assert len(store.get_items()) == 1
        assert len(store.get_items(list_name="grocery")) == 1

    def test_list_names_case_insensitive(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="Grocery")
        store.add("Eggs", list_name="grocery")
        # Both should end up on the same list (normalized to lowercase)
        assert len(store.get_items(list_name="grocery")) == 2

    def test_dedup_within_list_not_across(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", quantity=1, list_name="grocery")
        store.add("Milk", quantity=2, list_name="costco")
        assert store.get_items(list_name="grocery")[0]["quantity"] == 1
        assert store.get_items(list_name="costco")[0]["quantity"] == 2

    def test_remove_from_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Milk", list_name="costco")
        store.remove(["Milk"], list_name="grocery")
        assert len(store.get_items(list_name="grocery")) == 0
        assert len(store.get_items(list_name="costco")) == 1

    def test_check_on_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Milk", list_name="costco")
        store.check(["Milk"], list_name="grocery")
        grocery_milk = store.get_items(list_name="grocery")[0]
        costco_milk = store.get_items(list_name="costco")[0]
        assert grocery_milk["checked"] is True
        assert costco_milk["checked"] is False

    def test_uncheck_on_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.check(["Milk"], list_name="grocery")
        store.uncheck(["Milk"], list_name="grocery")
        assert store.get_items(list_name="grocery")[0]["checked"] is False

    def test_clear_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill", list_name="hardware")
        store.check(["Milk"], list_name="grocery")
        store.clear(checked_only=True, list_name="grocery")
        assert len(store.get_items(list_name="grocery")) == 0
        assert len(store.get_items(list_name="hardware")) == 1

    def test_get_list_names(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill", list_name="hardware")
        names = store.get_list_names()
        assert set(names) == {"grocery", "hardware"}

    def test_get_list_names_excludes_empty(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill", list_name="hardware")
        store.clear(checked_only=False, list_name="hardware")
        names = store.get_list_names()
        assert names == ["grocery"]

    def test_get_all_list_names_includes_empty(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill", list_name="hardware")
        store.clear(checked_only=False, list_name="hardware")
        names = store.get_all_list_names()
        assert set(names) == {"grocery", "hardware"}

    def test_delete_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill", list_name="hardware")
        assert store.delete_list("hardware") is True
        assert "hardware" not in store.get_all_list_names()
        assert store.delete_list("nonexistent") is False

    def test_migrate_legacy_format(self, tmp_path):
        """Loading a legacy single-list file migrates to multi-list format."""
        f = str(tmp_path / "shopping.json")
        legacy_data = {
            "items": [
                {"name": "Milk", "quantity": 1, "unit": "", "category": "Dairy",
                 "checked": False, "added_at": "2026-01-01T00:00:00+00:00", "added_by": ""},
            ]
        }
        with open(f, "w") as fh:
            json.dump(legacy_data, fh)
        store = shopping_list_server.ShoppingListStore(f)
        # Should have migrated to "grocery" list
        items = store.get_items(list_name="grocery")
        assert len(items) == 1
        assert items[0]["name"] == "Milk"
        # Verify on-disk format was updated
        with open(f) as fh:
            data = json.load(fh)
        assert "lists" in data
        assert "items" not in data

    def test_save_and_reload_multi_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        store = shopping_list_server.ShoppingListStore(f)
        store.add("Milk", list_name="grocery")
        store.add("Drill", list_name="hardware")
        store2 = shopping_list_server.ShoppingListStore(f)
        assert len(store2.get_items(list_name="grocery")) == 1
        assert len(store2.get_items(list_name="hardware")) == 1


# ---------------------------------------------------------------------------
# TestShoppingListAdd (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListAdd:
    @pytest.mark.asyncio
    async def test_add_single_item(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _add({"items": [{"name": "Eggs"}]})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "Eggs" in text

    @pytest.mark.asyncio
    async def test_add_multiple_items(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _add({
                "items": [
                    {"name": "Eggs", "quantity": 12, "unit": "", "category": "Dairy"},
                    {"name": "Milk", "quantity": 1, "unit": "gallon", "category": "Dairy"},
                ]
            })
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 2

    @pytest.mark.asyncio
    async def test_add_dedup_merge_same_unit(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs", "quantity": 2}]})
            result = await _add({"items": [{"name": "eggs", "quantity": 3}]})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 1
        assert data["lists"]["grocery"]["items"][0]["quantity"] == 5

    @pytest.mark.asyncio
    async def test_add_no_merge_different_units(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Milk", "quantity": 1, "unit": "cup"}]})
            await _add({"items": [{"name": "Milk", "quantity": 2, "unit": "tbsp"}]})
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 2

    @pytest.mark.asyncio
    async def test_add_to_named_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _add({"items": [{"name": "Drill bits"}], "list_name": "hardware"})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "hardware" in text
        assert "Drill bits" in text
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["hardware"]["items"]) == 1

    @pytest.mark.asyncio
    async def test_add_default_list_is_grocery(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}]})
        with open(f) as fh:
            data = json.load(fh)
        assert "grocery" in data["lists"]
        assert len(data["lists"]["grocery"]["items"]) == 1


# ---------------------------------------------------------------------------
# TestShoppingListView (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListView:
    @pytest.mark.asyncio
    async def test_view_empty(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _view({})
        assert not _is_error(result)
        data = json.loads(_parse_text(result))
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_view_all(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [
                {"name": "Eggs", "category": "Dairy"},
                {"name": "Bread", "category": "Bakery"},
            ]})
            result = await _view({})
        data = json.loads(_parse_text(result))
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_view_filter_category(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [
                {"name": "Eggs", "category": "Dairy"},
                {"name": "Bread", "category": "Bakery"},
            ]})
            result = await _view({"category": "Dairy"})
        data = json.loads(_parse_text(result))
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Eggs"

    @pytest.mark.asyncio
    async def test_view_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}], "list_name": "grocery"})
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            result = await _view({"list_name": "hardware"})
        data = json.loads(_parse_text(result))
        assert data["list_name"] == "hardware"
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Drill"

    @pytest.mark.asyncio
    async def test_view_includes_list_name(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _view({})
        data = json.loads(_parse_text(result))
        assert "list_name" in data


# ---------------------------------------------------------------------------
# TestShoppingListRemove (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListRemove:
    @pytest.mark.asyncio
    async def test_remove_existing(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}, {"name": "Milk"}]})
            result = await _remove({"names": ["Eggs"]})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 1
        assert data["lists"]["grocery"]["items"][0]["name"] == "Milk"

    @pytest.mark.asyncio
    async def test_remove_nonexistent_no_error(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}]})
            result = await _remove({"names": ["Bananas"]})
        assert not _is_error(result)

    @pytest.mark.asyncio
    async def test_remove_from_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Milk"}], "list_name": "grocery"})
            await _add({"items": [{"name": "Milk"}], "list_name": "costco"})
            result = await _remove({"names": ["Milk"], "list_name": "costco"})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 1
        assert len(data["lists"]["costco"]["items"]) == 0


# ---------------------------------------------------------------------------
# TestShoppingListCheck (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListCheck:
    @pytest.mark.asyncio
    async def test_check_existing(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}]})
            result = await _check({"names": ["Eggs"]})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert data["lists"]["grocery"]["items"][0]["checked"] is True

    @pytest.mark.asyncio
    async def test_check_on_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            result = await _check({"names": ["Drill"], "list_name": "hardware"})
        assert not _is_error(result)
        assert "hardware" in _parse_text(result)
        with open(f) as fh:
            data = json.load(fh)
        assert data["lists"]["hardware"]["items"][0]["checked"] is True


# ---------------------------------------------------------------------------
# TestShoppingListUncheck (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListUncheck:
    @pytest.mark.asyncio
    async def test_uncheck_item(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}]})
            await _check({"names": ["Eggs"]})
            result = await _uncheck({"names": ["Eggs"]})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert data["lists"]["grocery"]["items"][0]["checked"] is False

    @pytest.mark.asyncio
    async def test_uncheck_on_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            await _check({"names": ["Drill"], "list_name": "hardware"})
            result = await _uncheck({"names": ["Drill"], "list_name": "hardware"})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert data["lists"]["hardware"]["items"][0]["checked"] is False


# ---------------------------------------------------------------------------
# TestShoppingListClear (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListClear:
    @pytest.mark.asyncio
    async def test_clear_checked_only_default(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}, {"name": "Milk"}]})
            await _check({"names": ["Eggs"]})
            result = await _clear({})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 1
        assert data["lists"]["grocery"]["items"][0]["name"] == "Milk"

    @pytest.mark.asyncio
    async def test_clear_all(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}, {"name": "Milk"}]})
            await _check({"names": ["Eggs"]})
            result = await _clear({"all": True})
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 0

    @pytest.mark.asyncio
    async def test_clear_specific_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}], "list_name": "grocery"})
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            result = await _clear({"all": True, "list_name": "hardware"})
        assert not _is_error(result)
        assert "hardware" in _parse_text(result)
        with open(f) as fh:
            data = json.load(fh)
        assert len(data["lists"]["grocery"]["items"]) == 1
        assert len(data["lists"]["hardware"]["items"]) == 0


# ---------------------------------------------------------------------------
# TestShoppingListLists (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListLists:
    @pytest.mark.asyncio
    async def test_lists_empty(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _lists({})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "no" in text.lower() or "No" in text

    @pytest.mark.asyncio
    async def test_lists_with_items(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}, {"name": "Milk"}], "list_name": "grocery"})
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            result = await _lists({})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "grocery" in text
        assert "hardware" in text
        assert "2 item(s)" in text
        assert "1 item(s)" in text

    @pytest.mark.asyncio
    async def test_lists_excludes_empty_by_default(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}], "list_name": "grocery"})
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            await _clear({"all": True, "list_name": "hardware"})
            result = await _lists({})
        text = _parse_text(result)
        assert "grocery" in text
        assert "hardware" not in text

    @pytest.mark.asyncio
    async def test_lists_includes_empty_when_requested(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Eggs"}], "list_name": "grocery"})
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            await _clear({"all": True, "list_name": "hardware"})
            result = await _lists({"include_empty": True})
        text = _parse_text(result)
        assert "grocery" in text
        assert "hardware" in text


# ---------------------------------------------------------------------------
# TestShoppingListDeleteList (MCP tool)
# ---------------------------------------------------------------------------
class TestShoppingListDeleteList:
    @pytest.mark.asyncio
    async def test_delete_existing_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            await _add({"items": [{"name": "Drill"}], "list_name": "hardware"})
            result = await _delete_list({"list_name": "hardware"})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "hardware" in text.lower()
        with open(f) as fh:
            data = json.load(fh)
        assert "hardware" not in data["lists"]

    @pytest.mark.asyncio
    async def test_delete_nonexistent_list(self, tmp_path):
        f = str(tmp_path / "shopping.json")
        with patch.dict("os.environ", {"SHOPPING_LIST_FILE": f}):
            result = await _delete_list({"list_name": "nonexistent"})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "not found" in text.lower()


# ---------------------------------------------------------------------------
# TestBuildShoppingListBlocks
# ---------------------------------------------------------------------------
class TestBuildShoppingListBlocks:
    """Tests for build_shopping_list_blocks() which generates Slack Block Kit blocks."""

    def test_empty_list_returns_empty_message(self):
        """An empty item list produces a single section block saying the list is empty."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        blocks = build_shopping_list_blocks([])
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        text = blocks[0]["text"]["text"].lower()
        assert "empty" in text

    def test_empty_list_includes_list_name(self):
        """The empty list message includes the list name."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        blocks = build_shopping_list_blocks([], list_name="costco")
        text = blocks[0]["text"]["text"].lower()
        assert "costco" in text

    def test_unchecked_item_has_checkbox_option(self):
        """Unchecked items appear as checkbox options with name, quantity, unit."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        items = [
            {"name": "Milk", "quantity": 2, "unit": "gallon", "category": "Dairy", "checked": False},
        ]
        blocks = build_shopping_list_blocks(items)
        # Should have a header + actions block
        actions_blocks = [b for b in blocks if b["type"] == "actions"]
        assert len(actions_blocks) == 1
        checkboxes = actions_blocks[0]["elements"][0]
        assert checkboxes["type"] == "checkboxes"
        assert len(checkboxes["options"]) == 1
        option = checkboxes["options"][0]
        assert "Milk" in option["text"]["text"]
        assert "2" in option["text"]["text"]
        assert "gallon" in option["text"]["text"]
        assert option["value"] == "Milk"
        # Not checked, so no initial_options
        assert "initial_options" not in checkboxes

    def test_unchecked_item_without_unit(self):
        """An unchecked item without a unit shows name and quantity if > 1."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        items = [
            {"name": "Eggs", "quantity": 12, "unit": "", "category": "Dairy", "checked": False},
        ]
        blocks = build_shopping_list_blocks(items)
        actions_blocks = [b for b in blocks if b["type"] == "actions"]
        option = actions_blocks[0]["elements"][0]["options"][0]
        assert "Eggs" in option["text"]["text"]
        assert "12" in option["text"]["text"]
        assert option["value"] == "Eggs"

    def test_checked_item_in_initial_options(self):
        """Checked items appear in initial_options of their category's checkboxes."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        items = [
            {"name": "Bread", "quantity": 1, "unit": "", "category": "Bakery", "checked": True},
        ]
        blocks = build_shopping_list_blocks(items)
        actions_blocks = [b for b in blocks if b["type"] == "actions"]
        checkboxes = actions_blocks[0]["elements"][0]
        assert "initial_options" in checkboxes
        assert checkboxes["initial_options"][0]["value"] == "Bread"

    def test_mixed_checked_and_unchecked(self):
        """A mix of checked and unchecked items in the same category."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        items = [
            {"name": "Milk", "quantity": 1, "unit": "", "category": "Dairy", "checked": False},
            {"name": "Eggs", "quantity": 6, "unit": "", "category": "Dairy", "checked": True},
        ]
        blocks = build_shopping_list_blocks(items)
        actions_blocks = [b for b in blocks if b["type"] == "actions"]
        checkboxes = actions_blocks[0]["elements"][0]
        assert len(checkboxes["options"]) == 2
        # Only Eggs should be in initial_options
        assert len(checkboxes["initial_options"]) == 1
        assert checkboxes["initial_options"][0]["value"] == "Eggs"

    def test_items_grouped_by_category(self):
        """Items are grouped by category with header blocks."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        items = [
            {"name": "Milk", "quantity": 1, "unit": "gallon", "category": "Dairy", "checked": False},
            {"name": "Bread", "quantity": 1, "unit": "", "category": "Bakery", "checked": False},
        ]
        blocks = build_shopping_list_blocks(items)
        headers = [b for b in blocks if b["type"] == "header"]
        header_texts = [h["text"]["text"] for h in headers]
        assert "Dairy" in header_texts
        assert "Bakery" in header_texts

    def test_action_id_includes_list_name(self):
        """Action IDs include the list name for disambiguation."""
        from src.mcp.shopping_list_server import build_shopping_list_blocks

        items = [
            {"name": "Milk", "quantity": 1, "unit": "", "category": "Dairy", "checked": False},
        ]
        blocks = build_shopping_list_blocks(items, list_name="costco")
        actions_blocks = [b for b in blocks if b["type"] == "actions"]
        action_id = actions_blocks[0]["elements"][0]["action_id"]
        assert "costco" in action_id


# ---------------------------------------------------------------------------
# TestRecipeStore
# ---------------------------------------------------------------------------
class TestRecipeStore:
    def test_load_missing_file(self, tmp_path):
        """Loading from a non-existent file initializes with empty recipes list."""
        f = str(tmp_path / "nonexistent" / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        assert store.list_recipes() == []

    def test_load_empty_file(self, tmp_path):
        """Loading from an empty/missing path initializes with empty recipes list."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        assert store.list_recipes() == []

    def test_save_and_reload(self, tmp_path):
        """Round-trip persistence: save a recipe, create a new store from the same file."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="https://example.com/dumplings",
            ingredients=[{"name": "flour", "quantity": "2", "unit": "cups"}],
            instructions="Mix and cook.",
            added_by="U123",
        )
        store2 = shopping_list_server.RecipeStore(f)
        recipes = store2.list_recipes()
        assert len(recipes) == 1
        assert recipes[0]["name"] == "Easy Pan Dumplings"

    def test_save_recipe_creates_correct_fields(self, tmp_path):
        """save() creates a recipe with auto-generated id and all expected fields."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="https://example.com/dumplings",
            ingredients=[{"name": "flour", "quantity": "2", "unit": "cups"}],
            instructions="Mix and cook.",
            added_by="U123",
        )
        recipe = store.get_recipe("easy-pan-dumplings")
        assert recipe is not None
        assert recipe["id"] == "easy-pan-dumplings"
        assert recipe["name"] == "Easy Pan Dumplings"
        assert recipe["source_url"] == "https://example.com/dumplings"
        assert recipe["ingredients"] == [{"name": "flour", "quantity": "2", "unit": "cups"}]
        assert recipe["instructions"] == "Mix and cook."
        assert recipe["added_by"] == "U123"
        assert "added_at" in recipe

    def test_id_generation_slugifies_name(self, tmp_path):
        """'Easy Pan Dumplings' should produce id 'easy-pan-dumplings'."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="",
            ingredients=[],
            instructions="",
            added_by="",
        )
        recipe = store.get_recipe("easy-pan-dumplings")
        assert recipe is not None
        assert recipe["id"] == "easy-pan-dumplings"

    def test_overwrite_existing_recipe(self, tmp_path):
        """Saving a recipe with the same name overwrites the previous one."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="https://old.com",
            ingredients=[],
            instructions="Old instructions",
            added_by="U123",
        )
        store.save(
            name="Easy Pan Dumplings",
            source_url="https://new.com",
            ingredients=[{"name": "wrapper", "quantity": "1", "unit": "pack"}],
            instructions="New instructions",
            added_by="U456",
        )
        recipes = store.list_recipes()
        assert len(recipes) == 1
        recipe = store.get_recipe("easy-pan-dumplings")
        assert recipe["source_url"] == "https://new.com"
        assert recipe["instructions"] == "New instructions"

    def test_list_recipes_returns_summaries(self, tmp_path):
        """list_recipes() returns summary dicts with id, name, source_url, added_at only."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="https://example.com",
            ingredients=[{"name": "flour", "quantity": "2", "unit": "cups"}],
            instructions="Mix and cook.",
            added_by="U123",
        )
        recipes = store.list_recipes()
        assert len(recipes) == 1
        summary = recipes[0]
        assert set(summary.keys()) == {"id", "name", "source_url", "added_at"}
        assert summary["name"] == "Easy Pan Dumplings"

    def test_get_recipe_by_id(self, tmp_path):
        """get_recipe() with an exact id returns the full recipe."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="https://example.com",
            ingredients=[{"name": "flour", "quantity": "2", "unit": "cups"}],
            instructions="Mix and cook.",
            added_by="U123",
        )
        recipe = store.get_recipe("easy-pan-dumplings")
        assert recipe is not None
        assert recipe["name"] == "Easy Pan Dumplings"
        assert "ingredients" in recipe
        assert "instructions" in recipe

    def test_get_recipe_by_name_substring(self, tmp_path):
        """get_recipe() matches case-insensitively on name substring."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="",
            ingredients=[],
            instructions="",
            added_by="",
        )
        recipe = store.get_recipe("dumplings")
        assert recipe is not None
        assert recipe["name"] == "Easy Pan Dumplings"

    def test_get_recipe_nonexistent(self, tmp_path):
        """get_recipe() returns None for a non-existent recipe."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        assert store.get_recipe("nonexistent") is None

    def test_delete_existing(self, tmp_path):
        """delete() removes a recipe by id and returns True."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        store.save(
            name="Easy Pan Dumplings",
            source_url="",
            ingredients=[],
            instructions="",
            added_by="",
        )
        assert store.delete("easy-pan-dumplings") is True
        assert store.get_recipe("easy-pan-dumplings") is None
        assert store.list_recipes() == []

    def test_delete_nonexistent(self, tmp_path):
        """delete() returns False when the recipe doesn't exist."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        assert store.delete("nonexistent") is False


# ---------------------------------------------------------------------------
# TestRecipeTools (MCP tool functions)
# ---------------------------------------------------------------------------
class TestRecipeTools:
    @pytest.fixture(autouse=True)
    def _setup_recipe_store(self, tmp_path):
        """Create a fresh RecipeStore and patch _recipe_store for each test."""
        f = str(tmp_path / "recipes.json")
        store = shopping_list_server.RecipeStore(f)
        shopping_list_server._recipe_store = store
        yield
        shopping_list_server._recipe_store = None

    @pytest.mark.asyncio
    async def test_recipe_save(self):
        """recipe_save tool saves a recipe and returns success text."""
        _recipe_save = shopping_list_server.recipe_save.handler
        result = await _recipe_save({
            "name": "Easy Pan Dumplings",
            "source_url": "https://example.com/dumplings",
            "ingredients": [{"name": "flour", "quantity": "2", "unit": "cups"}],
            "instructions": "Mix and cook.",
            "added_by": "U123",
        })
        assert not _is_error(result)
        text = _parse_text(result)
        assert "Easy Pan Dumplings" in text

    @pytest.mark.asyncio
    async def test_recipe_list(self):
        """recipe_list tool returns a formatted list of saved recipes."""
        _recipe_save = shopping_list_server.recipe_save.handler
        _recipe_list = shopping_list_server.recipe_list.handler
        await _recipe_save({
            "name": "Easy Pan Dumplings",
            "source_url": "https://example.com",
            "ingredients": [],
            "instructions": "Cook it.",
            "added_by": "U123",
        })
        result = await _recipe_list({})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "Easy Pan Dumplings" in text

    @pytest.mark.asyncio
    async def test_recipe_list_empty(self):
        """recipe_list tool returns an appropriate message when no recipes exist."""
        _recipe_list = shopping_list_server.recipe_list.handler
        result = await _recipe_list({})
        assert not _is_error(result)
        text = _parse_text(result).lower()
        assert "no" in text or "empty" in text or "0" in text

    @pytest.mark.asyncio
    async def test_recipe_view(self):
        """recipe_view tool returns full recipe details for a matching query."""
        _recipe_save = shopping_list_server.recipe_save.handler
        _recipe_view = shopping_list_server.recipe_view.handler
        await _recipe_save({
            "name": "Easy Pan Dumplings",
            "source_url": "https://example.com",
            "ingredients": [{"name": "flour", "quantity": "2", "unit": "cups"}],
            "instructions": "Mix and cook.",
            "added_by": "U123",
        })
        result = await _recipe_view({"query": "dumplings"})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "Easy Pan Dumplings" in text
        assert "flour" in text

    @pytest.mark.asyncio
    async def test_recipe_view_not_found(self):
        """recipe_view tool returns an error for a non-matching query."""
        _recipe_view = shopping_list_server.recipe_view.handler
        result = await _recipe_view({"query": "nonexistent"})
        assert _is_error(result) or "not found" in _parse_text(result).lower()

    @pytest.mark.asyncio
    async def test_recipe_delete(self):
        """recipe_delete tool deletes a matching recipe and returns success."""
        _recipe_save = shopping_list_server.recipe_save.handler
        _recipe_delete = shopping_list_server.recipe_delete.handler
        await _recipe_save({
            "name": "Easy Pan Dumplings",
            "source_url": "",
            "ingredients": [],
            "instructions": "",
            "added_by": "",
        })
        result = await _recipe_delete({"query": "easy-pan-dumplings"})
        assert not _is_error(result)
        text = _parse_text(result)
        assert "delete" in text.lower() or "removed" in text.lower()

    @pytest.mark.asyncio
    async def test_recipe_delete_not_found(self):
        """recipe_delete tool returns an error for a non-matching query."""
        _recipe_delete = shopping_list_server.recipe_delete.handler
        result = await _recipe_delete({"query": "nonexistent"})
        assert _is_error(result) or "not found" in _parse_text(result).lower()
