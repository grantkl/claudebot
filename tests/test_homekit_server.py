"""Tests for HomeKit MCP server tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp.homekit_server import (
    homekit_identify,
    homekit_list_pairings,
    homekit_set_light,
)


@pytest.fixture
def mock_pairings():
    """Return sample pairing data."""
    return {
        "living_room": {"AccessoryPairingID": "AA:BB:CC:DD:EE:FF", "Connection": "IP"},
        "bedroom": {"AccessoryPairingID": "11:22:33:44:55:66", "Connection": "IP"},
    }


@pytest.fixture
def mock_accessories():
    """Return sample accessory list with a lightbulb."""
    return [
        {
            "aid": 1,
            "services": [
                {
                    "type": "3E",
                    "iid": 1,
                    "characteristics": [
                        {"type": "23", "iid": 2, "value": "Test Light", "perms": ["pr"]},
                    ],
                },
                {
                    "type": "43",
                    "iid": 10,
                    "characteristics": [
                        {"type": "25", "iid": 11, "value": True, "perms": ["pr", "pw"]},
                        {"type": "8", "iid": 12, "value": 100, "perms": ["pr", "pw"]},
                        {"type": "13", "iid": 13, "value": 0.0, "perms": ["pr", "pw"]},
                        {"type": "2F", "iid": 14, "value": 0.0, "perms": ["pr", "pw"]},
                    ],
                },
            ],
        }
    ]


class TestHomekitListPairings:
    @pytest.mark.asyncio
    async def test_returns_aliases(self, mock_pairings):
        with patch("src.mcp.homekit_server.list_aliases", return_value=list(mock_pairings.keys())):
            result = await homekit_list_pairings.handler({})

        assert "is_error" not in result
        aliases = json.loads(result["content"][0]["text"])
        assert set(aliases) == {"living_room", "bedroom"}

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        with patch("src.mcp.homekit_server.list_aliases", return_value=[]):
            result = await homekit_list_pairings.handler({})

        assert "is_error" not in result
        aliases = json.loads(result["content"][0]["text"])
        assert aliases == []


class TestHomekitSetLight:
    @pytest.mark.asyncio
    async def test_sets_light_characteristics(self, mock_accessories):
        mock_pairing = AsyncMock()
        mock_pairing.list_accessories_and_characteristics = AsyncMock(return_value=mock_accessories)
        mock_pairing.put_characteristics = AsyncMock(return_value={})

        mock_controller = AsyncMock()

        # Patch _get_controller_and_pairing to yield mock objects
        async def mock_get_cp(alias):
            yield mock_controller, mock_pairing

        with patch("src.mcp.homekit_server._get_controller_and_pairing", side_effect=mock_get_cp):
            result = await homekit_set_light.handler({
                "alias": "living_room",
                "aid": 1,
                "on": True,
                "brightness": 75,
            })

        assert "is_error" not in result
        assert "set" in result["content"][0]["text"].lower() or "light" in result["content"][0]["text"].lower()
        # Verify put_characteristics was called with on=True and brightness=75
        mock_pairing.put_characteristics.assert_called_once()
        chars = mock_pairing.put_characteristics.call_args[0][0]
        # Should have (aid, on_iid, True) and (aid, brightness_iid, 75)
        assert len(chars) == 2
        assert (1, 11, True) in chars
        assert (1, 12, 75) in chars

    @pytest.mark.asyncio
    async def test_error_alias_not_found(self):
        async def mock_get_cp(alias):
            raise ValueError(f"No pairing found for alias '{alias}'.")
            yield  # make it an async generator

        with patch("src.mcp.homekit_server._get_controller_and_pairing", side_effect=mock_get_cp):
            result = await homekit_set_light.handler({
                "alias": "nonexistent",
                "aid": 1,
                "on": True,
            })

        assert result.get("is_error") is True
        assert "nonexistent" in result["content"][0]["text"]


class TestHomekitIdentify:
    @pytest.mark.asyncio
    async def test_identify_works(self):
        mock_pairing = AsyncMock()
        mock_pairing.identify = AsyncMock()

        mock_controller = AsyncMock()

        async def mock_get_cp(alias):
            yield mock_controller, mock_pairing

        with patch("src.mcp.homekit_server._get_controller_and_pairing", side_effect=mock_get_cp):
            result = await homekit_identify.handler({"alias": "living_room"})

        assert "is_error" not in result
        assert "identify" in result["content"][0]["text"].lower()
        mock_pairing.identify.assert_called_once()

    @pytest.mark.asyncio
    async def test_identify_error_alias_not_found(self):
        async def mock_get_cp(alias):
            raise ValueError(f"No pairing found for alias '{alias}'.")
            yield

        with patch("src.mcp.homekit_server._get_controller_and_pairing", side_effect=mock_get_cp):
            result = await homekit_identify.handler({"alias": "missing"})

        assert result.get("is_error") is True
        assert "missing" in result["content"][0]["text"]
