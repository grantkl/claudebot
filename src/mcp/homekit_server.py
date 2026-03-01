"""HomeKit MCP server tools using aiohomekit."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from .homekit_pairing import get_pairing_file, list_aliases, load_pairings

logger = logging.getLogger(__name__)


async def _get_controller_and_pairing(alias: str) -> Any:
    """Load pairing data and create a controller for the given alias.

    Returns an async context manager tuple: (controller, pairing).
    The caller must use ``async with controller:`` to manage the lifecycle.
    """
    from aiohomekit import Controller

    pairings = load_pairings()
    if alias not in pairings:
        raise ValueError(
            f"No pairing found for alias '{alias}'. "
            "Run scripts/homekit-pair.py first."
        )

    controller = Controller()
    async with controller:
        pairing_file = str(get_pairing_file())
        controller.load_data(pairing_file)
        if alias not in controller.aliases:
            raise ValueError(
                f"Controller could not load pairing for alias '{alias}'."
            )
        yield controller, controller.aliases[alias]


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


# ---------------------------------------------------------------------------
# 1. homekit_list_pairings
# ---------------------------------------------------------------------------
@tool(
    "homekit_list_pairings",
    "List all paired HomeKit device aliases from the pairing file.",
    {},
)
async def homekit_list_pairings(args: dict[str, Any]) -> dict[str, Any]:
    try:
        aliases = list_aliases()
        return _text(json.dumps(aliases, indent=2))
    except Exception as e:
        return _error(f"Failed to list pairings: {e}")


# ---------------------------------------------------------------------------
# 2. homekit_list_devices
# ---------------------------------------------------------------------------
@tool(
    "homekit_list_devices",
    "For each paired HomeKit device, connect and retrieve the accessories list. Returns JSON with name, aid, and services for each accessory.",
    {},
)
async def homekit_list_devices(args: dict[str, Any]) -> dict[str, Any]:
    try:
        aliases = list_aliases()
        if not aliases:
            return _text("[]")

        results = []
        for alias in aliases:
            try:
                async for controller, pairing in _get_controller_and_pairing(alias):
                    accessories = await pairing.list_accessories_and_characteristics()
                    for acc in accessories:
                        aid = acc["aid"]
                        services = []
                        for svc in acc.get("services", []):
                            services.append({
                                "type": svc.get("type", "unknown"),
                                "iid": svc.get("iid"),
                            })
                        # Try to extract name from accessory info
                        name = alias
                        for svc in acc.get("services", []):
                            for char in svc.get("characteristics", []):
                                if char.get("type") == "23" or char.get("description", "").lower() == "name":
                                    name = char.get("value", alias)
                                    break
                        results.append({
                            "alias": alias,
                            "name": name,
                            "aid": aid,
                            "services": services,
                        })
            except Exception as e:
                results.append({
                    "alias": alias,
                    "error": str(e),
                })
        return _text(json.dumps(results, indent=2))
    except Exception as e:
        return _error(f"Failed to list devices: {e}")


# ---------------------------------------------------------------------------
# 3. homekit_get_accessory
# ---------------------------------------------------------------------------
@tool(
    "homekit_get_accessory",
    "Get full info for one HomeKit accessory including all services and characteristics.",
    {"alias": str, "aid": int},
)
async def homekit_get_accessory(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    aid = args["aid"]
    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            accessories = await pairing.list_accessories_and_characteristics()
            for acc in accessories:
                if acc["aid"] == aid:
                    return _text(json.dumps(acc, indent=2))
            return _error(f"Accessory with aid={aid} not found for alias '{alias}'.")
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to get accessory: {e}")


# ---------------------------------------------------------------------------
# 4. homekit_get_characteristic
# ---------------------------------------------------------------------------
@tool(
    "homekit_get_characteristic",
    "Read a specific HomeKit characteristic value by accessory id (aid) and instance id (iid).",
    {"alias": str, "aid": int, "iid": int},
)
async def homekit_get_characteristic(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    aid = args["aid"]
    iid = args["iid"]
    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            result = await pairing.get_characteristics([(aid, iid)])
            key = (aid, iid)
            if key in result:
                return _text(json.dumps(result[key], indent=2, default=str))
            return _error(f"Characteristic ({aid}, {iid}) not found.")
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to get characteristic: {e}")


# ---------------------------------------------------------------------------
# 5. homekit_set_characteristic
# ---------------------------------------------------------------------------
@tool(
    "homekit_set_characteristic",
    "Set a specific HomeKit characteristic value. The value can be any JSON type.",
    {
        "type": "object",
        "properties": {
            "alias": {"type": "string"},
            "aid": {"type": "integer"},
            "iid": {"type": "integer"},
            "value": {},
        },
        "required": ["alias", "aid", "iid", "value"],
    },
)
async def homekit_set_characteristic(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    aid = args["aid"]
    iid = args["iid"]
    value = args["value"]
    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            result = await pairing.put_characteristics([(aid, iid, value)])
            if result:
                return _text(json.dumps(
                    {str(k): v for k, v in result.items()},
                    indent=2,
                    default=str,
                ))
            return _text(f"Set ({aid}, {iid}) = {value}")
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to set characteristic: {e}")


# ---------------------------------------------------------------------------
# 6. homekit_identify
# ---------------------------------------------------------------------------
@tool(
    "homekit_identify",
    "Trigger the identify action on a paired HomeKit accessory.",
    {"alias": str},
)
async def homekit_identify(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            await pairing.identify()
            return _text(f"Identify triggered for '{alias}'.")
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to identify: {e}")


# ---------------------------------------------------------------------------
# 7. homekit_set_light
# ---------------------------------------------------------------------------
@tool(
    "homekit_set_light",
    "Control a HomeKit lightbulb. Set on/off, brightness, hue, and saturation.",
    {
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "Pairing alias"},
            "aid": {"type": "integer", "description": "Accessory ID"},
            "on": {"type": "boolean", "description": "Turn light on or off"},
            "brightness": {"type": "integer", "description": "Brightness 0-100 (optional)"},
            "hue": {"type": "number", "description": "Hue 0-360 (optional)"},
            "saturation": {"type": "number", "description": "Saturation 0-100 (optional)"},
        },
        "required": ["alias", "aid", "on"],
    },
)
async def homekit_set_light(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    aid = args["aid"]
    on_value = args["on"]
    brightness = args.get("brightness")
    hue = args.get("hue")
    saturation = args.get("saturation")

    # HomeKit characteristic types for lightbulb service
    # These are well-known HAP characteristic UUIDs (short form):
    # On: 25, Brightness: 8, Hue: 13, Saturation: 2F
    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            # First get accessories to find the lightbulb service IIDs
            accessories = await pairing.list_accessories_and_characteristics()
            target_acc = None
            for acc in accessories:
                if acc["aid"] == aid:
                    target_acc = acc
                    break
            if not target_acc:
                return _error(f"Accessory with aid={aid} not found.")

            # Find lightbulb service (type 43 = Lightbulb)
            char_map: dict[str, int] = {}
            for svc in target_acc.get("services", []):
                svc_type = svc.get("type", "")
                # Lightbulb service type is 00000043 or just 43
                if svc_type.lstrip("0") in ("43",) or "lightbulb" in svc_type.lower():
                    for char in svc.get("characteristics", []):
                        ct = char.get("type", "").lstrip("0")
                        iid = char["iid"]
                        if ct == "25":
                            char_map["on"] = iid
                        elif ct == "8":
                            char_map["brightness"] = iid
                        elif ct == "13":
                            char_map["hue"] = iid
                        elif ct == "2F" or ct == "2f":
                            char_map["saturation"] = iid
                    break

            if "on" not in char_map:
                return _error(
                    f"No lightbulb service with On characteristic found on aid={aid}."
                )

            characteristics: list[tuple[int, int, Any]] = [
                (aid, char_map["on"], on_value)
            ]
            if brightness is not None and "brightness" in char_map:
                characteristics.append((aid, char_map["brightness"], brightness))
            if hue is not None and "hue" in char_map:
                characteristics.append((aid, char_map["hue"], hue))
            if saturation is not None and "saturation" in char_map:
                characteristics.append((aid, char_map["saturation"], saturation))

            result = await pairing.put_characteristics(characteristics)
            if result:
                return _text(json.dumps(
                    {str(k): v for k, v in result.items()},
                    indent=2,
                    default=str,
                ))
            return _text(f"Light on aid={aid} set: on={on_value}" +
                         (f", brightness={brightness}" if brightness is not None else "") +
                         (f", hue={hue}" if hue is not None else "") +
                         (f", saturation={saturation}" if saturation is not None else ""))
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to set light: {e}")


# ---------------------------------------------------------------------------
# 8. homekit_set_thermostat
# ---------------------------------------------------------------------------
@tool(
    "homekit_set_thermostat",
    "Control a HomeKit thermostat. Set target temperature and optionally the heating/cooling mode.",
    {
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "Pairing alias"},
            "aid": {"type": "integer", "description": "Accessory ID"},
            "target_temp": {"type": "number", "description": "Target temperature in Celsius"},
            "mode": {
                "type": "string",
                "description": "Mode: off, heat, cool, auto (optional)",
                "enum": ["off", "heat", "cool", "auto"],
            },
        },
        "required": ["alias", "aid", "target_temp"],
    },
)
async def homekit_set_thermostat(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    aid = args["aid"]
    target_temp = args["target_temp"]
    mode = args.get("mode")

    # Thermostat characteristic types:
    # Target Temperature: 35
    # Current Heating Cooling State: F
    # Target Heating Cooling State: 33
    MODE_MAP = {"off": 0, "heat": 1, "cool": 2, "auto": 3}

    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            accessories = await pairing.list_accessories_and_characteristics()
            target_acc = None
            for acc in accessories:
                if acc["aid"] == aid:
                    target_acc = acc
                    break
            if not target_acc:
                return _error(f"Accessory with aid={aid} not found.")

            # Find thermostat service (type 4A)
            char_map: dict[str, int] = {}
            for svc in target_acc.get("services", []):
                svc_type = svc.get("type", "")
                if svc_type.lstrip("0") in ("4A", "4a") or "thermostat" in svc_type.lower():
                    for char in svc.get("characteristics", []):
                        ct = char.get("type", "").lstrip("0")
                        iid = char["iid"]
                        if ct == "35":
                            char_map["target_temp"] = iid
                        elif ct == "33":
                            char_map["target_mode"] = iid
                    break

            if "target_temp" not in char_map:
                return _error(
                    f"No thermostat service with Target Temperature found on aid={aid}."
                )

            characteristics: list[tuple[int, int, Any]] = [
                (aid, char_map["target_temp"], target_temp)
            ]
            if mode is not None and "target_mode" in char_map:
                mode_val = MODE_MAP.get(mode)
                if mode_val is None:
                    return _error(f"Unknown mode '{mode}'. Use: off, heat, cool, auto.")
                characteristics.append((aid, char_map["target_mode"], mode_val))

            result = await pairing.put_characteristics(characteristics)
            if result:
                return _text(json.dumps(
                    {str(k): v for k, v in result.items()},
                    indent=2,
                    default=str,
                ))
            msg = f"Thermostat on aid={aid} set: target_temp={target_temp}"
            if mode is not None:
                msg += f", mode={mode}"
            return _text(msg)
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to set thermostat: {e}")


# ---------------------------------------------------------------------------
# 9. homekit_set_lock
# ---------------------------------------------------------------------------
@tool(
    "homekit_set_lock",
    "Control a HomeKit lock. Set locked or unlocked state.",
    {"alias": str, "aid": int, "locked": bool},
)
async def homekit_set_lock(args: dict[str, Any]) -> dict[str, Any]:
    alias = args["alias"]
    aid = args["aid"]
    locked = args["locked"]

    # Lock characteristic types:
    # Lock Target State: 1E (0=Unsecured, 1=Secured)
    try:
        async for controller, pairing in _get_controller_and_pairing(alias):
            accessories = await pairing.list_accessories_and_characteristics()
            target_acc = None
            for acc in accessories:
                if acc["aid"] == aid:
                    target_acc = acc
                    break
            if not target_acc:
                return _error(f"Accessory with aid={aid} not found.")

            # Find lock mechanism service (type 45)
            lock_iid = None
            for svc in target_acc.get("services", []):
                svc_type = svc.get("type", "")
                if svc_type.lstrip("0") in ("45",) or "lock" in svc_type.lower():
                    for char in svc.get("characteristics", []):
                        ct = char.get("type", "").lstrip("0")
                        if ct in ("1E", "1e"):
                            lock_iid = char["iid"]
                            break
                    break

            if lock_iid is None:
                return _error(
                    f"No lock service with Lock Target State found on aid={aid}."
                )

            value = 1 if locked else 0
            result = await pairing.put_characteristics([(aid, lock_iid, value)])
            if result:
                return _text(json.dumps(
                    {str(k): v for k, v in result.items()},
                    indent=2,
                    default=str,
                ))
            state = "locked" if locked else "unlocked"
            return _text(f"Lock on aid={aid} set to {state}.")
        return _error(f"Could not connect to alias '{alias}'.")
    except Exception as e:
        return _error(f"Failed to set lock: {e}")


# ---------------------------------------------------------------------------
# 10. homekit_trigger_scene
# ---------------------------------------------------------------------------
@tool(
    "homekit_trigger_scene",
    "Trigger a HomeKit scene by name. Searches all paired accessories for a matching scene.",
    {"scene_name": str},
)
async def homekit_trigger_scene(args: dict[str, Any]) -> dict[str, Any]:
    scene_name = args["scene_name"]
    try:
        aliases = list_aliases()
        if not aliases:
            return _error("No paired devices found.")

        for alias in aliases:
            try:
                async for controller, pairing in _get_controller_and_pairing(alias):
                    accessories = await pairing.list_accessories_and_characteristics()
                    for acc in accessories:
                        for svc in acc.get("services", []):
                            svc_type = svc.get("type", "")
                            # Service type for scenes is not a standard HAP service.
                            # Look for services that have a ConfiguredName or Name
                            # matching the scene name.
                            for char in svc.get("characteristics", []):
                                char_val = char.get("value", "")
                                if (
                                    isinstance(char_val, str)
                                    and char_val.lower() == scene_name.lower()
                                ):
                                    # Found a matching name — look for a programmable
                                    # switch or scene-active characteristic to trigger
                                    for trigger_char in svc.get("characteristics", []):
                                        perms = trigger_char.get("perms", [])
                                        if "pw" in perms:
                                            aid = acc["aid"]
                                            iid = trigger_char["iid"]
                                            await pairing.put_characteristics(
                                                [(aid, iid, 1)]
                                            )
                                            return _text(
                                                f"Scene '{scene_name}' triggered on "
                                                f"alias='{alias}', aid={aid}."
                                            )
            except Exception:
                continue

        return _error(
            f"Scene '{scene_name}' not found on any paired device. "
            "Scenes may not be directly exposed via HomeKit accessories."
        )
    except Exception as e:
        return _error(f"Failed to trigger scene: {e}")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
HOMEKIT_TOOLS: list[SdkMcpTool] = [
    homekit_list_pairings,
    homekit_list_devices,
    homekit_get_accessory,
    homekit_get_characteristic,
    homekit_set_characteristic,
    homekit_identify,
    homekit_set_light,
    homekit_set_thermostat,
    homekit_set_lock,
    homekit_trigger_scene,
]
