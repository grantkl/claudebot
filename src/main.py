"""Entry point for the ClaudeBot Slack application."""

import asyncio
import logging
import signal

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .claude_client import ClaudeManager
from .config import load_config
from .rate_limiter import RateLimiter
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

    scheduler = None
    if config.scheduler_enabled:
        from .scheduler import TaskScheduler

        scheduler = TaskScheduler(
            config, manager, config.slack_bot_token,
            config.scheduler_tasks_file, config.scheduler_state_file,
        )
        if config.enable_mcp:
            from .mcp.scheduler_server import SCHEDULER_TOOLS, set_scheduler
            from claude_agent_sdk import create_sdk_mcp_server

            set_scheduler(scheduler)
            manager._mcp_servers["scheduler"] = create_sdk_mcp_server(
                name="scheduler", version="1.0.0", tools=SCHEDULER_TOOLS,
            )
        await scheduler.start()

    rate_limiter = RateLimiter(config.rate_limit_messages, config.rate_limit_window_seconds)
    app = create_app(config, manager, rate_limiter)
    handler = AsyncSocketModeHandler(app, config.slack_app_token)

    loop = asyncio.get_running_loop()

    async def shutdown() -> None:
        logger.info("Shutting down...")
        if scheduler is not None:
            await scheduler.stop()
        await manager.stop()
        await handler.close_async()
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.ensure_future(shutdown())
        )

    logger.info("ClaudeBot started successfully")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
