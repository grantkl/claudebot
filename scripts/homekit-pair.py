#!/usr/bin/env python3
"""Interactive CLI for one-time HomeKit device pairing."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to path so we can import the pairing module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp.homekit_pairing import get_pairing_file, list_aliases, load_pairings, save_pairings


async def _make_controller():
    """Create a Controller with its own zeroconf + service browser."""
    from aiohomekit import Controller
    from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser

    azc = AsyncZeroconf()
    hap_types = ["_hap._tcp.local.", "_hap._udp.local."]
    browser = AsyncServiceBrowser(azc.zeroconf, hap_types, handlers=[lambda **_: None])
    controller = Controller(azc)
    return azc, browser, controller


async def cmd_discover(args: argparse.Namespace) -> None:
    """Discover unpaired HomeKit accessories on the network."""
    azc, browser, controller = await _make_controller()

    print("Discovering HomeKit accessories (10s timeout)...")
    async with controller:
        found = []
        async for discovery in controller.async_discover(timeout=10):
            desc = discovery.description
            found.append({
                "name": desc.name,
                "id": desc.id,
                "category": str(desc.category),
                "paired": discovery.paired,
            })
            status = "paired" if discovery.paired else "unpaired"
            print(f"  {desc.name} ({desc.id}) [{status}]")

        if not found:
            print("No accessories found.")
        else:
            print(f"\nFound {len(found)} accessory(ies).")


async def cmd_pair(args: argparse.Namespace) -> None:
    """Pair a HomeKit device and save under alias."""
    device_id = args.device_id
    pin = args.pin
    alias = args.alias

    pairings = load_pairings()
    if alias in pairings:
        print(f"Error: alias '{alias}' already exists. Unpair first or use a different alias.")
        sys.exit(1)

    print(f"Pairing with device '{device_id}' as alias '{alias}'...")
    azc, browser, controller = await _make_controller()
    async with controller:
        discovery = await controller.async_find(device_id, timeout=10)
        finish = await discovery.async_start_pairing(alias)
        pairing = await finish(pin)
        print(f"Paired successfully as '{alias}'!")

        # Save pairing data to file
        # The controller now holds the pairing — save all data
        controller.save_data(str(get_pairing_file()))
        print(f"Pairing data saved to {get_pairing_file()}")


def cmd_list(args: argparse.Namespace) -> None:
    """List all paired device aliases."""
    aliases = list_aliases()
    if not aliases:
        print("No paired devices.")
        return
    print("Paired devices:")
    for alias in aliases:
        print(f"  - {alias}")


async def cmd_unpair(args: argparse.Namespace) -> None:
    """Remove a pairing."""
    alias = args.alias
    pairings = load_pairings()
    if alias not in pairings:
        print(f"Error: alias '{alias}' not found.")
        sys.exit(1)

    print(f"Removing pairing for '{alias}'...")
    try:
        azc, browser, controller = await _make_controller()
        async with controller:
            controller.load_data(str(get_pairing_file()))
            if alias in controller.aliases:
                await controller.remove_pairing(alias)
                controller.save_data(str(get_pairing_file()))
                print(f"Unpairing complete. Removed '{alias}' from device and pairing file.")
            else:
                # Pairing data exists in file but controller couldn't load it.
                # Remove it from the file directly.
                del pairings[alias]
                save_pairings(pairings)
                print(f"Removed '{alias}' from pairing file (device was unreachable).")
    except Exception as e:
        print(f"Warning: could not cleanly unpair from device: {e}")
        print("Removing from local pairing file anyway.")
        del pairings[alias]
        save_pairings(pairings)
        print(f"Removed '{alias}' from pairing file.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HomeKit pairing management CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # discover
    subparsers.add_parser("discover", help="Discover HomeKit accessories on the network")

    # pair
    pair_parser = subparsers.add_parser("pair", help="Pair a HomeKit device")
    pair_parser.add_argument("device_id", help="Device ID (from discover output)")
    pair_parser.add_argument("pin", help="HomeKit setup code (e.g. 123-45-678)")
    pair_parser.add_argument("alias", help="Friendly alias to save the pairing under")

    # list
    subparsers.add_parser("list", help="List all paired device aliases")

    # unpair
    unpair_parser = subparsers.add_parser("unpair", help="Remove a pairing")
    unpair_parser.add_argument("alias", help="Alias to remove")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "discover":
        asyncio.run(cmd_discover(args))
    elif args.command == "pair":
        asyncio.run(cmd_pair(args))
    elif args.command == "unpair":
        asyncio.run(cmd_unpair(args))


if __name__ == "__main__":
    main()
