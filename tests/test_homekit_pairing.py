"""Tests for HomeKit pairing data management."""

import json

from src.mcp.homekit_pairing import get_pairing_file, list_aliases, load_pairings


def test_load_pairings_empty(tmp_path, monkeypatch):
    """load_pairings returns empty dict when pairing file does not exist."""
    monkeypatch.setenv("HOMEKIT_PAIRING_FILE", str(tmp_path / "missing.json"))
    assert load_pairings() == {}


def test_load_pairings_with_data(tmp_path, monkeypatch):
    """load_pairings reads and parses JSON pairing file."""
    pairing_file = tmp_path / "pairings.json"
    data = {
        "living_room": {"AccessoryPairingID": "AA:BB:CC:DD:EE:FF", "Connection": "IP"},
        "bedroom": {"AccessoryPairingID": "11:22:33:44:55:66", "Connection": "IP"},
    }
    pairing_file.write_text(json.dumps(data))
    monkeypatch.setenv("HOMEKIT_PAIRING_FILE", str(pairing_file))
    result = load_pairings()
    assert result == data


def test_list_aliases_empty(tmp_path, monkeypatch):
    """list_aliases returns empty list when no pairings exist."""
    monkeypatch.setenv("HOMEKIT_PAIRING_FILE", str(tmp_path / "missing.json"))
    assert list_aliases() == []


def test_list_aliases_with_data(tmp_path, monkeypatch):
    """list_aliases returns alias names from pairing file."""
    pairing_file = tmp_path / "pairings.json"
    data = {
        "living_room": {"AccessoryPairingID": "AA:BB:CC:DD:EE:FF"},
        "bedroom": {"AccessoryPairingID": "11:22:33:44:55:66"},
    }
    pairing_file.write_text(json.dumps(data))
    monkeypatch.setenv("HOMEKIT_PAIRING_FILE", str(pairing_file))
    aliases = list_aliases()
    assert set(aliases) == {"living_room", "bedroom"}


def test_get_pairing_file_default(monkeypatch):
    """get_pairing_file returns default path when env var not set."""
    monkeypatch.delenv("HOMEKIT_PAIRING_FILE", raising=False)
    path = get_pairing_file()
    assert path.name == ".homekit_pairings.json"


def test_get_pairing_file_custom(tmp_path, monkeypatch):
    """get_pairing_file returns custom path from env var."""
    custom = str(tmp_path / "custom.json")
    monkeypatch.setenv("HOMEKIT_PAIRING_FILE", custom)
    assert str(get_pairing_file()) == custom
