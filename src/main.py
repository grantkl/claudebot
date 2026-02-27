"""Entry point for the ClaudeBot Slack application."""

import asyncio
import logging
import signal

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .claude_client import ClaudeManager
from .config import load_config
from .slack_app import create_app


async def main() -> None:
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("claudebot")

    manager = ClaudeManager(config)
    await manager.start()

    app = create_app(config, manager)
    handler = AsyncSocketModeHandler(app, config.slack_app_token)

    loop = asyncio.get_running_loop()

    async def shutdown() -> None:
        logger.info("Shutting down...")
        await manager.stop()
        await handler.close_async()  # type: ignore[no-untyped-call]
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.ensure_future(shutdown())
        )

    logger.info("ClaudeBot started successfully")
    await handler.start_async()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    asyncio.run(main())
