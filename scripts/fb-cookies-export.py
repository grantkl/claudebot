#!/usr/bin/env python3
"""One-time Facebook session capture for the fb_marketplace MCP.

Opens a Playwright Chromium window, lets you log into Facebook manually,
and then writes the resulting storage state (cookies + localStorage) to
``data/fb_cookies.json`` in the Playwright ``storage_state`` format that
the MCP loads.

Usage:
    python scripts/fb-cookies-export.py
    # optional:
    python scripts/fb-cookies-export.py --output data/fb_cookies.json

Notes:
  - You must have a regular Facebook account. Two-factor prompts work fine
    in the browser window.
  - Re-run when the MCP starts returning "Facebook redirected to login" —
    typically every few weeks as FB rotates session tokens.
  - The resulting JSON contains live session credentials. Treat it like a
    password: do not commit it to git (``data/`` is in .gitignore).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


DEFAULT_OUTPUT = "data/fb_cookies.json"


async def _capture(output: str) -> int:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        print(
            "Missing dependency: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

        print(
            "\nA Chromium window is open.\n"
            "  1. Log into Facebook (email + password, plus 2FA if enabled).\n"
            "  2. Confirm you can see your news feed or Marketplace.\n"
            "  3. Come back here and press ENTER to save cookies.\n"
        )
        # Block on stdin so the user can interact with the browser freely.
        await asyncio.to_thread(input, "Press ENTER when logged in... ")

        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        await context.storage_state(path=output)
        await browser.close()

    print(f"\nWrote Facebook session to {output}")
    print("Set FB_COOKIES_FILE in your env if this is not the default path.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Where to write the Playwright storage_state JSON (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_capture(args.output)))


if __name__ == "__main__":
    main()
