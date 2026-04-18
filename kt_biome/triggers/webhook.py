"""Webhook Trigger — receive HTTP POST requests as agent events.

Starts a lightweight HTTP server that listens for incoming webhooks.
Each POST body becomes a TriggerEvent that wakes the agent.

Usage in config.yaml:

    triggers:
      - type: custom
        module: kt_biome.triggers.webhook
        class_name: WebhookTrigger
        options:
          port: 9090
          path: /webhook
          secret: ""  # optional HMAC secret for verification

Example webhook call:
    curl -X POST http://localhost:9090/webhook \
         -H "Content-Type: application/json" \
         -d '{"message": "deploy completed", "service": "api"}'
"""

import asyncio
import hashlib
import hmac
import json
from typing import Any

from kohakuterrarium.core.events import EventType, TriggerEvent
from kohakuterrarium.modules.trigger.base import BaseTrigger
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


class WebhookTrigger(BaseTrigger):
    """HTTP webhook trigger — POST requests become agent events."""

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._port = int(opts.get("port", 9090))
        self._path = opts.get("path", "/webhook")
        self._secret = opts.get("secret", "")
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self._server = None

    async def _on_start(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_post(self._path, self._handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        self._server = web.TCPSite(runner, "0.0.0.0", self._port)
        await self._server.start()
        logger.info(
            "Webhook trigger listening",
            port=self._port,
            path=self._path,
        )

    async def _on_stop(self) -> None:
        if self._server:
            await self._server.stop()
            logger.info("Webhook trigger stopped")

    async def _handle_webhook(self, request):
        from aiohttp import web

        body = await request.read()

        # Verify HMAC signature if secret is configured
        if self._secret:
            sig_header = request.headers.get("X-Signature-256", "")
            expected = (
                "sha256="
                + hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()
            )
            if not hmac.compare_digest(sig_header, expected):
                return web.Response(status=403, text="Invalid signature")

        # Parse body
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"raw": body.decode("utf-8", errors="replace")}

        # Build event
        content = data.get("message", "") or data.get("content", "") or json.dumps(data)
        event = TriggerEvent(
            type=EventType.EXTERNAL,
            content=str(content),
            source="webhook",
            metadata=data,
        )
        await self._queue.put(event)
        logger.debug("Webhook received", content=str(content)[:100])

        return web.json_response({"status": "ok"})

    async def wait_for_trigger(self) -> TriggerEvent:
        return await self._queue.get()
