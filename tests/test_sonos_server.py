"""Tests for the Sonos MCP server tools."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# The @tool decorator from claude_agent_sdk must preserve the original function
# as .handler so tests can call it.  Other test files inject a plain MagicMock
# for claude_agent_sdk which turns every decorated function into a MagicMock.
# Fix: install a proper fake before (re-)importing sonos_server.

class _FakeSdkMcpTool:
    def __init__(self, handler: Any, name: str, description: str, schema: Any) -> None:
        self.handler = handler
        self.name = name
        self.description = description
        self.schema = schema


def _fake_tool(name: str, description: str, schema: Any):  # noqa: ANN201
    def decorator(fn: Any) -> _FakeSdkMcpTool:
        return _FakeSdkMcpTool(fn, name, description, schema)
    return decorator


_mock_sdk = MagicMock()
_mock_sdk.tool = _fake_tool
_mock_sdk.SdkMcpTool = _FakeSdkMcpTool
sys.modules["claude_agent_sdk"] = _mock_sdk

# Mock soco and its submodules (not installed in test environment)
sys.modules.setdefault("soco", MagicMock())
sys.modules.setdefault("soco.plugins", MagicMock())
sys.modules.setdefault("soco.plugins.sharelink", MagicMock())

# Force reimport so sonos_server picks up the proper fake decorator
sys.modules.pop("src.mcp.sonos_server", None)

from src.mcp import sonos_server  # noqa: E402

# The @tool decorator wraps functions into SdkMcpTool objects.
# Access the underlying async handler via the .handler attribute.
_discover = sonos_server.sonos_discover.handler
_get_state = sonos_server.sonos_get_state.handler
_play = sonos_server.sonos_play.handler
_pause = sonos_server.sonos_pause.handler
_stop = sonos_server.sonos_stop.handler
_next = sonos_server.sonos_next.handler
_previous = sonos_server.sonos_previous.handler
_set_volume = sonos_server.sonos_set_volume.handler
_play_favorite = sonos_server.sonos_play_favorite.handler
_list_favorites = sonos_server.sonos_list_favorites.handler
_play_uri = sonos_server.sonos_play_uri.handler
_group_speakers = sonos_server.sonos_group_speakers.handler
_ungroup_speaker = sonos_server.sonos_ungroup_speaker.handler
_set_sleep_timer = sonos_server.sonos_set_sleep_timer.handler
_list_queue = sonos_server.sonos_list_queue.handler


def _make_speaker(
    name: str = "Living Room",
    ip: str = "192.168.1.10",
    volume: int = 30,
    is_coordinator: bool = True,
) -> MagicMock:
    """Create a mock SoCo speaker."""
    speaker = MagicMock()
    speaker.player_name = name
    speaker.ip_address = ip
    speaker.volume = volume
    speaker.is_coordinator = is_coordinator
    speaker.speaker_info = {"model_name": "Sonos One"}
    group = MagicMock()
    group.label = "Living Room"
    speaker.group = group
    speaker.get_current_track_info.return_value = {
        "title": "Test Song",
        "artist": "Test Artist",
        "album": "Test Album",
        "position": "0:01:30",
        "duration": "0:04:00",
    }
    speaker.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING",
    }
    return speaker


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


# ---------------------------------------------------------------------------
# _get_all_speakers
# ---------------------------------------------------------------------------
class TestGetAllSpeakers:
    @patch.dict(os.environ, {"SONOS_SPEAKER_IPS": "192.168.1.10,192.168.1.11"})
    @patch("src.mcp.sonos_server.soco.SoCo")
    def test_uses_configured_ips(self, mock_soco: MagicMock) -> None:
        s1 = _make_speaker("Living Room", "192.168.1.10")
        s2 = _make_speaker("Bedroom", "192.168.1.11")
        mock_instance = MagicMock()
        mock_instance.all_zones = {s1, s2}
        mock_soco.return_value = mock_instance

        result = sonos_server._get_all_speakers()

        assert result == {s1, s2}
        mock_soco.assert_called_once_with("192.168.1.10")

    @patch.dict(os.environ, {"SONOS_SPEAKER_IPS": ""})
    @patch("src.mcp.sonos_server.soco.discover")
    def test_falls_back_to_discover(self, mock_discover: MagicMock) -> None:
        speakers = {_make_speaker()}
        mock_discover.return_value = speakers

        result = sonos_server._get_all_speakers()

        assert result == speakers
        mock_discover.assert_called_once_with(timeout=5)

    @patch.dict(os.environ, {}, clear=False)
    @patch("src.mcp.sonos_server.soco.discover")
    def test_no_env_var_falls_back_to_discover(self, mock_discover: MagicMock) -> None:
        os.environ.pop("SONOS_SPEAKER_IPS", None)
        speakers = {_make_speaker()}
        mock_discover.return_value = speakers

        result = sonos_server._get_all_speakers()

        assert result == speakers
        mock_discover.assert_called_once_with(timeout=5)

    @patch.dict(os.environ, {"SONOS_SPEAKER_IPS": "192.168.1.10"})
    @patch("src.mcp.sonos_server.soco.SoCo")
    def test_unreachable_ip_returns_empty(self, mock_soco: MagicMock) -> None:
        mock_soco.return_value.all_zones = None

        result = sonos_server._get_all_speakers()

        assert result == set()

    @patch.dict(os.environ, {"SONOS_SPEAKER_IPS": "192.168.1.10,192.168.1.11"})
    @patch("src.mcp.sonos_server.soco.SoCo")
    def test_first_ip_fails_tries_next(self, mock_soco: MagicMock) -> None:
        s1 = _make_speaker("Living Room", "192.168.1.11")
        good_instance = MagicMock()
        good_instance.all_zones = {s1}

        # First call raises, second succeeds
        mock_soco.side_effect = [Exception("Connection refused"), good_instance]

        result = sonos_server._get_all_speakers()

        assert result == {s1}
        assert mock_soco.call_count == 2


# ---------------------------------------------------------------------------
# sonos_discover
# ---------------------------------------------------------------------------
class TestSonosDiscover:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_discovers_speakers(self, mock_get_all: MagicMock) -> None:
        s1 = _make_speaker("Living Room", "192.168.1.10")
        s2 = _make_speaker("Bedroom", "192.168.1.11", volume=20, is_coordinator=False)
        mock_get_all.return_value = {s1, s2}

        result = await _discover({})
        data = json.loads(_parse_text(result))

        assert len(data) == 2
        names = {d["name"] for d in data}
        assert names == {"Living Room", "Bedroom"}
        by_name = {d["name"]: d for d in data}
        assert by_name["Living Room"]["volume"] == 30
        assert by_name["Bedroom"]["is_coordinator"] is False
        assert not _is_error(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_no_speakers_returns_empty_list(
        self, mock_get_all: MagicMock
    ) -> None:
        mock_get_all.return_value = set()

        result = await _discover({})
        data = json.loads(_parse_text(result))

        assert data == []
        assert not _is_error(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_discovery_error_returns_error(
        self, mock_get_all: MagicMock
    ) -> None:
        mock_get_all.side_effect = Exception("Network error")

        result = await _discover({})
        assert _is_error(result)
        assert "Discovery failed" in _parse_text(result)


# ---------------------------------------------------------------------------
# sonos_get_state
# ---------------------------------------------------------------------------
class TestSonosGetState:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_returns_current_state(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _get_state({"speaker_name": "Living Room"})
        data = json.loads(_parse_text(result))

        assert data["speaker"] == "Living Room"
        assert data["play_state"] == "PLAYING"
        assert data["volume"] == 30
        assert data["track"]["title"] == "Test Song"
        assert data["track"]["artist"] == "Test Artist"
        assert not _is_error(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_speaker_not_found(self, mock_get_all: MagicMock) -> None:
        mock_get_all.return_value = {_make_speaker("Kitchen")}

        result = await _get_state({"speaker_name": "Bathroom"})
        assert _is_error(result)
        assert "not found" in _parse_text(result)


# ---------------------------------------------------------------------------
# sonos_play / pause / stop
# ---------------------------------------------------------------------------
class TestPlaybackControls:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_play(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _play({"speaker_name": "Living Room"})
        assert not _is_error(result)
        assert "Playing" in _parse_text(result)
        speaker.play.assert_called_once()

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_pause(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _pause({"speaker_name": "Living Room"})
        assert not _is_error(result)
        assert "Paused" in _parse_text(result)
        speaker.pause.assert_called_once()

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_stop(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _stop({"speaker_name": "Living Room"})
        assert not _is_error(result)
        assert "Stopped" in _parse_text(result)
        speaker.stop.assert_called_once()

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_play_speaker_not_found(self, mock_get_all: MagicMock) -> None:
        mock_get_all.return_value = set()

        result = await _play({"speaker_name": "Nonexistent"})
        assert _is_error(result)
        assert "not found" in _parse_text(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_next(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _next({"speaker_name": "Living Room"})
        assert not _is_error(result)
        speaker.next.assert_called_once()

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_previous(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _previous({"speaker_name": "Living Room"})
        assert not _is_error(result)
        speaker.previous.assert_called_once()


# ---------------------------------------------------------------------------
# sonos_set_volume
# ---------------------------------------------------------------------------
class TestSetVolume:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_sets_volume(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_volume({"speaker_name": "Living Room", "volume": 50})
        assert not _is_error(result)
        assert "50" in _parse_text(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_volume_out_of_range_low(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_volume(
            {"speaker_name": "Living Room", "volume": -1}
        )
        assert _is_error(result)
        assert "between 0 and 100" in _parse_text(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_volume_out_of_range_high(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_volume(
            {"speaker_name": "Living Room", "volume": 101}
        )
        assert _is_error(result)
        assert "between 0 and 100" in _parse_text(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_volume_boundary_zero(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_volume({"speaker_name": "Living Room", "volume": 0})
        assert not _is_error(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_volume_boundary_hundred(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_volume(
            {"speaker_name": "Living Room", "volume": 100}
        )
        assert not _is_error(result)


# ---------------------------------------------------------------------------
# sonos_play_favorite
# ---------------------------------------------------------------------------
class TestPlayFavorite:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_plays_matching_favorite(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        fav = MagicMock()
        fav.title = "Chill Vibes Playlist"
        fav.get_uri.return_value = "x-rincon-cpcontainer:abc123"
        fav.resource_meta_data = "<meta/>"
        speaker.music_library.get_sonos_favorites.return_value = [fav]

        result = await _play_favorite(
            {"speaker_name": "Living Room", "favorite_name": "chill vibes"}
        )
        assert not _is_error(result)
        assert "Chill Vibes Playlist" in _parse_text(result)
        speaker.play_uri.assert_called_once()

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_favorite_not_found(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        fav = MagicMock()
        fav.title = "Rock Classics"
        speaker.music_library.get_sonos_favorites.return_value = [fav]

        result = await _play_favorite(
            {"speaker_name": "Living Room", "favorite_name": "jazz"}
        )
        assert _is_error(result)
        assert "not found" in _parse_text(result)
        assert "Rock Classics" in _parse_text(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_fuzzy_match_case_insensitive(
        self, mock_get_all: MagicMock
    ) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        fav = MagicMock()
        fav.title = "My Morning Jazz"
        fav.get_uri.return_value = "x-rincon-cpcontainer:xyz"
        fav.resource_meta_data = "<meta/>"
        speaker.music_library.get_sonos_favorites.return_value = [fav]

        result = await _play_favorite(
            {"speaker_name": "Living Room", "favorite_name": "MORNING JAZZ"}
        )
        assert not _is_error(result)
        assert "My Morning Jazz" in _parse_text(result)


# ---------------------------------------------------------------------------
# sonos_group_speakers / sonos_ungroup_speaker
# ---------------------------------------------------------------------------
class TestGrouping:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_group_speakers(self, mock_get_all: MagicMock) -> None:
        coordinator = _make_speaker("Living Room", "192.168.1.10")
        member = _make_speaker("Bedroom", "192.168.1.11")
        mock_get_all.return_value = {coordinator, member}

        result = await _group_speakers(
            {"coordinator_name": "Living Room", "member_names": ["Bedroom"]}
        )
        assert not _is_error(result)
        assert "Grouped" in _parse_text(result)
        member.join.assert_called_once_with(coordinator)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_group_speaker_not_found(self, mock_get_all: MagicMock) -> None:
        coordinator = _make_speaker("Living Room", "192.168.1.10")
        mock_get_all.return_value = {coordinator}

        result = await _group_speakers(
            {"coordinator_name": "Living Room", "member_names": ["Nonexistent"]}
        )
        assert _is_error(result)
        assert "not found" in _parse_text(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_ungroup_speaker(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _ungroup_speaker({"speaker_name": "Living Room"})
        assert not _is_error(result)
        assert "Ungrouped" in _parse_text(result)
        speaker.unjoin.assert_called_once()


# ---------------------------------------------------------------------------
# sonos_set_sleep_timer
# ---------------------------------------------------------------------------
class TestSleepTimer:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_set_timer(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_sleep_timer(
            {"speaker_name": "Living Room", "minutes": 30}
        )
        assert not _is_error(result)
        assert "30 minutes" in _parse_text(result)
        speaker.set_sleep_timer.assert_called_once_with(1800)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_cancel_timer(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _set_sleep_timer(
            {"speaker_name": "Living Room", "minutes": 0}
        )
        assert not _is_error(result)
        assert "cancelled" in _parse_text(result)
        speaker.set_sleep_timer.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# sonos_list_queue
# ---------------------------------------------------------------------------
class TestListQueue:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_lists_queue_items(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        item1 = MagicMock()
        item1.title = "Song 1"
        item1.creator = "Artist 1"
        item1.album = "Album 1"
        item2 = MagicMock()
        item2.title = "Song 2"
        item2.creator = "Artist 2"
        item2.album = "Album 2"
        speaker.get_queue.return_value = [item1, item2]

        result = await _list_queue({"speaker_name": "Living Room"})
        data = json.loads(_parse_text(result))

        assert len(data) == 2
        assert data[0]["title"] == "Song 1"
        assert data[1]["artist"] == "Artist 2"
        assert not _is_error(result)


# ---------------------------------------------------------------------------
# sonos_play_uri
# ---------------------------------------------------------------------------
class TestPlayUri:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_plays_uri(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _play_uri(
            {
                "speaker_name": "Living Room",
                "uri": "x-rincon-mp3radio://example.com/stream",
                "title": "My Stream",
            }
        )
        assert not _is_error(result)
        speaker.play_uri.assert_called_once()

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_plays_uri_without_title(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        result = await _play_uri(
            {
                "speaker_name": "Living Room",
                "uri": "x-rincon-mp3radio://example.com/stream",
            }
        )
        assert not _is_error(result)
        speaker.play_uri.assert_called_once()


# ---------------------------------------------------------------------------
# sonos_list_favorites
# ---------------------------------------------------------------------------
class TestListFavorites:
    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_lists_favorites(self, mock_get_all: MagicMock) -> None:
        speaker = _make_speaker()
        mock_get_all.return_value = {speaker}

        fav = MagicMock()
        fav.title = "My Playlist"
        fav.get_uri.return_value = "x-rincon-cpcontainer:abc"
        speaker.music_library.get_sonos_favorites.return_value = [fav]

        result = await _list_favorites({})
        data = json.loads(_parse_text(result))

        assert len(data) == 1
        assert data[0]["title"] == "My Playlist"
        assert not _is_error(result)

    @patch("src.mcp.sonos_server._get_all_speakers")
    async def test_no_speakers_returns_error(self, mock_get_all: MagicMock) -> None:
        mock_get_all.return_value = set()

        result = await _list_favorites({})
        assert _is_error(result)
        assert "No Sonos speakers found" in _parse_text(result)
