"""Naive Discord I/O — use Discord as a simple chat interface.

A minimal Discord input/output pair that lets you run any creature
with Discord as the interface. No fancy features — just message in,
response out.

For production Discord bots with media handling, dedup, templates,
and drop mechanics, see ``examples/agent-apps/discord_bot/``.

Usage in config.yaml:

    input:
      type: custom
      module: kt_defaults.io.discord
      class_name: DiscordInput
      options:
        token_env: DISCORD_BOT_TOKEN
        channel_ids: [123456789]

    output:
      type: custom
      module: kt_defaults.io.discord
      class_name: DiscordOutput
      options:
        token_env: DISCORD_BOT_TOKEN
        channel_ids: [123456789]

Requires: pip install discord.py
"""

import asyncio
import os
from typing import Any

from kohakuterrarium.core.events import EventType, TriggerEvent
from kohakuterrarium.modules.input.base import BaseInputModule
from kohakuterrarium.modules.output.base import BaseOutputModule
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

# Shared client instance (input and output share the same bot connection)
_client = None
_client_lock = asyncio.Lock()


async def _get_client(token: str):
    """Get or create the shared Discord client."""
    global _client
    async with _client_lock:
        if _client is not None:
            return _client

        try:
            import discord
        except ImportError:
            raise ImportError(
                "discord.py is required for Discord I/O. "
                "Install with: pip install discord.py"
            )

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        ready_event = asyncio.Event()

        @client.event
        async def on_ready():
            logger.info("Discord bot connected", user=str(client.user))
            ready_event.set()

        asyncio.create_task(client.start(token))
        await asyncio.wait_for(ready_event.wait(), timeout=30)
        _client = client
        return client


class DiscordInput(BaseInputModule):
    """Receive messages from Discord channels."""

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._token_env = opts.get("token_env", "DISCORD_BOT_TOKEN")
        self._channel_ids = set(int(c) for c in opts.get("channel_ids", []))
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self._client = None
        self._handler_registered = False

    async def _on_start(self) -> None:
        token = os.environ.get(self._token_env, "")
        if not token:
            raise ValueError(
                f"Discord bot token not found in env var: {self._token_env}"
            )
        self._client = await _get_client(token)
        if not self._handler_registered:
            self._handler_registered = True

            @self._client.event
            async def on_message(message):
                # Ignore own messages
                if message.author == self._client.user:
                    return
                # Filter by channel
                if self._channel_ids and message.channel.id not in self._channel_ids:
                    return
                # Create trigger event
                content = message.content or ""
                if not content.strip():
                    return
                event = TriggerEvent(
                    type=EventType.USER_INPUT,
                    content=content,
                    source="discord",
                    metadata={
                        "author": str(message.author),
                        "channel": str(message.channel),
                        "channel_id": message.channel.id,
                        "message_id": message.id,
                    },
                )
                await self._queue.put(event)

    async def _on_stop(self) -> None:
        pass  # Client is shared, don't close it here

    async def get_input(self) -> TriggerEvent:
        return await self._queue.get()


class DiscordOutput(BaseOutputModule):
    """Send agent responses to Discord channels."""

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._token_env = opts.get("token_env", "DISCORD_BOT_TOKEN")
        self._channel_ids = [int(c) for c in opts.get("channel_ids", [])]
        self._client = None
        self._buffer = ""
        self._target_channel = None

    async def start(self) -> None:
        token = os.environ.get(self._token_env, "")
        if not token:
            raise ValueError(
                f"Discord bot token not found in env var: {self._token_env}"
            )
        self._client = await _get_client(token)
        if self._channel_ids:
            self._target_channel = self._client.get_channel(self._channel_ids[0])

    async def stop(self) -> None:
        await self.flush()

    async def write(self, text: str) -> None:
        self._buffer += text
        # Send if buffer is getting large (Discord has 2000 char limit)
        if len(self._buffer) > 1800:
            await self.flush()

    async def write_stream(self, chunk: str) -> None:
        self._buffer += chunk

    async def flush(self) -> None:
        if not self._buffer.strip() or not self._target_channel:
            self._buffer = ""
            return
        text = self._buffer.strip()
        self._buffer = ""
        # Split into 2000-char chunks if needed
        while text:
            chunk = text[:2000]
            text = text[2000:]
            try:
                await self._target_channel.send(chunk)
            except Exception as e:
                logger.error("Discord send failed", error=str(e))

    async def on_processing_start(self) -> None:
        if self._target_channel:
            try:
                await self._target_channel.typing()
            except Exception:
                pass

    async def on_processing_end(self) -> None:
        await self.flush()
