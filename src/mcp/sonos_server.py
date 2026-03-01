"""Sonos MCP server tools for controlling Sonos speakers via soco."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any

import soco
from soco.plugins.sharelink import ShareLinkPlugin

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)


def _get_all_speakers() -> set[soco.SoCo]:
    """Discover all Sonos speakers, using configured IPs if available."""
    configured_ips = os.environ.get("SONOS_SPEAKER_IPS", "")
    if configured_ips.strip():
        ips = [ip.strip() for ip in configured_ips.split(",") if ip.strip()]
        logger.debug("Using configured Sonos speaker IPs: %s", ips)
        for ip in ips:
            try:
                speaker = soco.SoCo(ip)
                zones: set[soco.SoCo] = speaker.all_zones
                if zones:
                    return zones
            except Exception:
                logger.debug("Could not reach Sonos speaker at %s, trying next", ip)
                continue
        logger.warning("None of the configured Sonos IPs were reachable")
        return set()
    logger.debug("No SONOS_SPEAKER_IPS configured, falling back to network discovery")
    return soco.discover(timeout=5) or set()


def _find_speaker(name: str) -> soco.SoCo:
    """Find speaker by name (case-insensitive). Raises ValueError if not found.

    When multiple speakers share the same name (e.g. a grouped home-theatre
    setup where the Arc, surrounds and sub all report the same room name),
    the group coordinator is preferred so that playback-state queries and
    transport commands are always directed at the right device.
    """
    matches = [s for s in _get_all_speakers() if s.player_name.lower() == name.lower()]
    if not matches:
        raise ValueError(f"Speaker '{name}' not found")
    for speaker in matches:
        if speaker.is_coordinator:
            return speaker
    return matches[0]


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


async def _run_sync(fn: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, fn)


# ---------------------------------------------------------------------------
# 1. sonos_discover
# ---------------------------------------------------------------------------
@tool(
    "sonos_discover",
    "Discover all Sonos speakers on the network. Returns a JSON list with name, ip, model, volume, is_coordinator, and group_label for each speaker.",
    {},
)
async def sonos_discover(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speakers = await _run_sync(_get_all_speakers)
        if not speakers:
            return _text("[]")
        result = []
        for s in speakers:
            result.append(
                {
                    "name": s.player_name,
                    "ip": s.ip_address,
                    "model": s.speaker_info.get("model_name", "unknown"),
                    "volume": s.volume,
                    "is_coordinator": s.is_coordinator,
                    "group_label": s.group.label if s.group else None,
                }
            )
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Discovery failed: {e}")


# ---------------------------------------------------------------------------
# 2. sonos_get_state
# ---------------------------------------------------------------------------
@tool(
    "sonos_get_state",
    "Get current playback state of a Sonos speaker including track info, volume, play state, and position.",
    {"speaker_name": str},
)
async def sonos_get_state(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        info = await _run_sync(lambda: speaker.get_current_track_info())
        transport = await _run_sync(lambda: speaker.get_current_transport_info())
        state = {
            "speaker": speaker.player_name,
            "play_state": transport.get("current_transport_state", "UNKNOWN"),
            "volume": speaker.volume,
            "track": {
                "title": info.get("title", ""),
                "artist": info.get("artist", ""),
                "album": info.get("album", ""),
                "position": info.get("position", ""),
                "duration": info.get("duration", ""),
            },
        }
        return _text(json.dumps(state, indent=2))
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to get state: {e}")


# ---------------------------------------------------------------------------
# 3. sonos_play
# ---------------------------------------------------------------------------
@tool(
    "sonos_play",
    "Resume playback on a Sonos speaker.",
    {"speaker_name": str},
)
async def sonos_play(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: speaker.play())
        return _text(f"Playing on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to play: {e}")


# ---------------------------------------------------------------------------
# 4. sonos_pause
# ---------------------------------------------------------------------------
@tool(
    "sonos_pause",
    "Pause playback on a Sonos speaker.",
    {"speaker_name": str},
)
async def sonos_pause(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: speaker.pause())
        return _text(f"Paused {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to pause: {e}")


# ---------------------------------------------------------------------------
# 5. sonos_stop
# ---------------------------------------------------------------------------
@tool(
    "sonos_stop",
    "Stop playback on a Sonos speaker.",
    {"speaker_name": str},
)
async def sonos_stop(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: speaker.stop())
        return _text(f"Stopped {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to stop: {e}")


# ---------------------------------------------------------------------------
# 6. sonos_next
# ---------------------------------------------------------------------------
@tool(
    "sonos_next",
    "Skip to the next track on a Sonos speaker.",
    {"speaker_name": str},
)
async def sonos_next(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: speaker.next())
        return _text(f"Skipped to next track on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to skip: {e}")


# ---------------------------------------------------------------------------
# 7. sonos_previous
# ---------------------------------------------------------------------------
@tool(
    "sonos_previous",
    "Go to the previous track on a Sonos speaker.",
    {"speaker_name": str},
)
async def sonos_previous(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: speaker.previous())
        return _text(f"Went to previous track on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to go to previous: {e}")


# ---------------------------------------------------------------------------
# 8. sonos_set_volume
# ---------------------------------------------------------------------------
@tool(
    "sonos_set_volume",
    "Set the volume of a Sonos speaker (0-100).",
    {
        "type": "object",
        "properties": {
            "speaker_name": {"type": "string"},
            "volume": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "required": ["speaker_name", "volume"],
    },
)
async def sonos_set_volume(args: dict[str, Any]) -> dict[str, Any]:
    try:
        volume = args["volume"]
        if not (0 <= volume <= 100):
            return _error("Volume must be between 0 and 100")
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: setattr(speaker, "volume", volume))
        return _text(f"Volume set to {volume} on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to set volume: {e}")


# ---------------------------------------------------------------------------
# 9. sonos_play_favorite
# ---------------------------------------------------------------------------
@tool(
    "sonos_play_favorite",
    "Play a Sonos favorite by name (case-insensitive fuzzy match). Use sonos_list_favorites to see available favorites.",
    {"speaker_name": str, "favorite_name": str},
)
async def sonos_play_favorite(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        favs = await _run_sync(lambda: speaker.music_library.get_sonos_favorites())
        target = args["favorite_name"].lower()
        match = None
        for fav in favs:
            if target in fav.title.lower():
                match = fav
                break
        if not match:
            available = [f.title for f in favs]
            return _error(
                f"Favorite '{args['favorite_name']}' not found. "
                f"Available: {available}"
            )
        uri = match.get_uri()
        meta = match.resource_meta_data
        await _run_sync(
            lambda: speaker.play_uri(uri=uri, meta=meta, title=match.title)
        )
        return _text(f"Playing favorite '{match.title}' on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to play favorite: {e}")


# ---------------------------------------------------------------------------
# 10. sonos_list_favorites
# ---------------------------------------------------------------------------
@tool(
    "sonos_list_favorites",
    "List all Sonos favorites.",
    {},
)
async def sonos_list_favorites(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speakers = await _run_sync(_get_all_speakers)
        if not speakers:
            return _error("No Sonos speakers found")
        speaker = next(iter(speakers))
        favs = await _run_sync(lambda: speaker.music_library.get_sonos_favorites())
        result = [{"title": f.title, "uri": f.get_uri()} for f in favs]
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to list favorites: {e}")


# ---------------------------------------------------------------------------
# 11. sonos_play_uri
# ---------------------------------------------------------------------------
@tool(
    "sonos_play_uri",
    "Play a specific URI on a Sonos speaker.",
    {
        "type": "object",
        "properties": {
            "speaker_name": {"type": "string"},
            "uri": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["speaker_name", "uri"],
    },
)
async def sonos_play_uri(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        title = args.get("title", "")
        await _run_sync(lambda: speaker.play_uri(uri=args["uri"], title=title))
        return _text(f"Playing URI on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to play URI: {e}")


# ---------------------------------------------------------------------------
# 12. sonos_group_speakers
# ---------------------------------------------------------------------------
@tool(
    "sonos_group_speakers",
    "Group Sonos speakers together. The coordinator controls playback for the group.",
    {
        "type": "object",
        "properties": {
            "coordinator_name": {"type": "string"},
            "member_names": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["coordinator_name", "member_names"],
    },
)
async def sonos_group_speakers(args: dict[str, Any]) -> dict[str, Any]:
    try:
        coordinator = await _run_sync(
            lambda: _find_speaker(args["coordinator_name"])
        )
        joined = []
        for name in args["member_names"]:
            member = await _run_sync(lambda: _find_speaker(name))
            await _run_sync(lambda: member.join(coordinator))
            joined.append(name)
        return _text(
            f"Grouped {joined} with coordinator {coordinator.player_name}"
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to group speakers: {e}")


# ---------------------------------------------------------------------------
# 13. sonos_ungroup_speaker
# ---------------------------------------------------------------------------
@tool(
    "sonos_ungroup_speaker",
    "Remove a Sonos speaker from its current group.",
    {"speaker_name": str},
)
async def sonos_ungroup_speaker(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        await _run_sync(lambda: speaker.unjoin())
        return _text(f"Ungrouped {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to ungroup: {e}")


# ---------------------------------------------------------------------------
# 14. sonos_set_sleep_timer
# ---------------------------------------------------------------------------
@tool(
    "sonos_set_sleep_timer",
    "Set a sleep timer on a Sonos speaker. Duration in minutes. Set to 0 to cancel.",
    {
        "type": "object",
        "properties": {
            "speaker_name": {"type": "string"},
            "minutes": {"type": "integer", "minimum": 0},
        },
        "required": ["speaker_name", "minutes"],
    },
)
async def sonos_set_sleep_timer(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        minutes = args["minutes"]
        if minutes == 0:
            await _run_sync(lambda: speaker.set_sleep_timer(None))
            return _text(f"Sleep timer cancelled on {speaker.player_name}")
        duration = minutes * 60
        await _run_sync(lambda: speaker.set_sleep_timer(duration))
        return _text(
            f"Sleep timer set to {minutes} minutes on {speaker.player_name}"
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to set sleep timer: {e}")


# ---------------------------------------------------------------------------
# 15. sonos_list_queue
# ---------------------------------------------------------------------------
@tool(
    "sonos_list_queue",
    "List items in the playback queue of a Sonos speaker.",
    {
        "type": "object",
        "properties": {
            "speaker_name": {"type": "string"},
            "start": {"type": "integer", "minimum": 0},
            "count": {"type": "integer", "minimum": 1},
        },
        "required": ["speaker_name"],
    },
)
async def sonos_list_queue(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        start = args.get("start", 0)
        count = args.get("count", 20)
        queue = await _run_sync(
            lambda: speaker.get_queue(start=start, max_items=count)
        )
        items = []
        for i, item in enumerate(queue, start=start):
            items.append(
                {
                    "position": i,
                    "title": item.title,
                    "artist": getattr(item, "creator", ""),
                    "album": getattr(item, "album", ""),
                }
            )
        return _text(json.dumps(items, indent=2))
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to list queue: {e}")


# ---------------------------------------------------------------------------
# 16. sonos_search_apple_music
# ---------------------------------------------------------------------------
@tool(
    "sonos_search_apple_music",
    "Search the Apple Music catalog. Returns matching songs with title, artist, album, and URL. Use sonos_play_apple_music to play a result.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (song name, artist, etc.)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Max results (default 5)"},
        },
        "required": ["query"],
    },
)
async def sonos_search_apple_music(args: dict[str, Any]) -> dict[str, Any]:
    try:
        query = args["query"]
        limit = args.get("limit", 5)
        params = urllib.parse.urlencode(
            {"term": query, "media": "music", "entity": "song", "country": "us", "limit": limit}
        )
        url = f"https://itunes.apple.com/search?{params}"

        def _fetch() -> list[dict[str, Any]]:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            return [
                {
                    "title": r["trackName"],
                    "artist": r["artistName"],
                    "album": r["collectionName"],
                    "url": r["trackViewUrl"],
                }
                for r in data.get("results", [])
            ]

        results = await _run_sync(_fetch)
        if not results:
            return _error(f"No results found for '{query}'")
        return _text(json.dumps(results, indent=2))
    except Exception as e:
        return _error(f"Search failed: {e}")


# ---------------------------------------------------------------------------
# 17. sonos_play_apple_music
# ---------------------------------------------------------------------------
@tool(
    "sonos_play_apple_music",
    "Play an Apple Music song, album, or playlist on a Sonos speaker. Accepts an Apple Music URL (from sonos_search_apple_music) or a search query. Requires Apple Music to be linked in the Sonos app.",
    {
        "type": "object",
        "properties": {
            "speaker_name": {"type": "string"},
            "url": {"type": "string", "description": "Apple Music URL (e.g., https://music.apple.com/us/album/...)"},
            "query": {"type": "string", "description": "Search query — used if url is not provided"},
        },
        "required": ["speaker_name"],
    },
)
async def sonos_play_apple_music(args: dict[str, Any]) -> dict[str, Any]:
    try:
        speaker = await _run_sync(lambda: _find_speaker(args["speaker_name"]))
        url = args.get("url")

        if not url:
            query = args.get("query")
            if not query:
                return _error("Either 'url' or 'query' is required")
            params = urllib.parse.urlencode(
                {"term": query, "media": "music", "entity": "song", "country": "us", "limit": 1}
            )
            api_url = f"https://itunes.apple.com/search?{params}"

            def _fetch() -> str | None:
                with urllib.request.urlopen(api_url, timeout=10) as resp:
                    data = json.loads(resp.read())
                results = data.get("results", [])
                return results[0]["trackViewUrl"] if results else None

            url = await _run_sync(_fetch)
            if not url:
                return _error(f"No Apple Music results for '{query}'")

        plugin = ShareLinkPlugin(speaker)
        if not plugin.is_share_link(url):
            return _error(f"Not a valid Apple Music URL: {url}")

        queue_pos = await _run_sync(lambda: plugin.add_share_link_to_queue(url))
        await _run_sync(lambda: speaker.play_from_queue(queue_pos - 1))

        return _text(f"Playing Apple Music on {speaker.player_name}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to play Apple Music: {e}")


# ---------------------------------------------------------------------------
# Export all tools
# ---------------------------------------------------------------------------
SONOS_TOOLS: list[SdkMcpTool] = [
    sonos_discover,
    sonos_get_state,
    sonos_play,
    sonos_pause,
    sonos_stop,
    sonos_next,
    sonos_previous,
    sonos_set_volume,
    sonos_play_favorite,
    sonos_list_favorites,
    sonos_play_uri,
    sonos_group_speakers,
    sonos_ungroup_speaker,
    sonos_set_sleep_timer,
    sonos_list_queue,
    sonos_search_apple_music,
    sonos_play_apple_music,
]
