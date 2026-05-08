"""Telegram input module.

Receives Telegram messages via long-polling and converts each one to a
``TriggerEvent(type="user_input")`` for the KohakuTerrarium controller.

Usage in ``config.yaml``:

    input:
      type: custom
      module: kt_biome.io.telegram_input
      class_name: TelegramInput
      options:
        token: "${TELEGRAM_BOT_TOKEN}"   # or a literal string
        allow_chat_ids: []               # empty = allow any
        allow_user_ids: []               # empty = allow any
        command_prefix: ""               # e.g. "/ask" — drops other messages
        dm_only: true
        include_attachments: true

Requires: ``pip install python-telegram-bot>=21``. The dependency is
OPTIONAL — the module imports it lazily in ``start()`` so the rest of
kt-biome still loads without it installed.

Webhook mode is not implemented yet; long-polling covers the common
deployment. Webhook support is tracked as future work.
"""

import asyncio
import os
import re
from typing import Any

from kohakuterrarium.core.events import EventType, TriggerEvent
from kohakuterrarium.llm.message import ImagePart, TextPart
from kohakuterrarium.modules.input.base import BaseInputModule
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Env-var expansion
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def expand_env_var(value: str) -> str:
    """Expand a ``${VAR}`` reference to its environment value.

    A plain string is returned unchanged. A ``${VAR}`` reference that is
    not set raises ``ValueError`` with a clear message. This is used for
    the ``token`` option so missing secrets fail loudly at startup
    rather than silently producing an unauthenticated bot.
    """
    if not isinstance(value, str):
        return value
    match = _ENV_PATTERN.match(value.strip())
    if not match:
        return value
    var_name = match.group(1)
    resolved = os.environ.get(var_name)
    if resolved is None or resolved == "":
        raise ValueError(
            f"Environment variable {var_name!r} is not set; "
            f"required for telegram_input token configuration."
        )
    return resolved


# ---------------------------------------------------------------------------
# SDK availability check
# ---------------------------------------------------------------------------


def _check_sdk() -> None:
    """Raise ``ImportError`` with install hint when python-telegram-bot
    is missing.

    Called at ``start()`` rather than at import time so that kt-biome
    itself loads without the optional dependency.
    """
    try:
        import telegram  # noqa: F401
        import telegram.ext  # noqa: F401
    except ImportError as exc:  # pragma: no cover - only exercised when SDK missing
        raise ImportError(
            "python-telegram-bot (>=21) is required for TelegramInput. "
            "Install with: pip install 'python-telegram-bot>=21'"
        ) from exc


def is_sdk_available() -> bool:
    """Return True when python-telegram-bot is importable."""
    try:
        import telegram  # noqa: F401
        import telegram.ext  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Input module
# ---------------------------------------------------------------------------


class TelegramInput(BaseInputModule):
    """Long-polling Telegram input module."""

    def __init__(self, options: dict[str, Any] | None = None):
        super().__init__()
        opts = options or {}
        self._token_raw: str = str(opts.get("token", ""))
        self._allow_chat_ids: set[int] = {
            int(c) for c in opts.get("allow_chat_ids") or []
        }
        self._allow_user_ids: set[int] = {
            int(u) for u in opts.get("allow_user_ids") or []
        }
        self._command_prefix: str = str(opts.get("command_prefix", "") or "")
        self._dm_only: bool = bool(opts.get("dm_only", True))
        self._include_attachments: bool = bool(opts.get("include_attachments", True))

        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self._application: Any = None
        self._poll_task: asyncio.Task[Any] | None = None
        self._resolved_token: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _on_start(self) -> None:
        if self._application is not None:
            # Idempotent — already running.
            return

        _check_sdk()
        self._resolved_token = expand_env_var(self._token_raw)
        if not self._resolved_token:
            raise ValueError(
                "TelegramInput: 'token' is empty. "
                "Set it to your bot token or ${ENV_VAR_NAME}."
            )

        from telegram.ext import Application, MessageHandler, filters

        self._application = Application.builder().token(self._resolved_token).build()
        self._application.add_handler(MessageHandler(filters.ALL, self._handle_message))

        # Kick off the polling loop in a background task so start() returns
        # immediately. We build the coroutine manually instead of calling
        # run_polling(), which is blocking/synchronous.
        self._poll_task = asyncio.create_task(
            self._run_polling(), name="telegram-input-poll"
        )
        logger.info("Telegram input started", mode="long-polling")

    async def _run_polling(self) -> None:
        """Initialize the Application, start polling, keep running."""
        app = self._application
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            # Stay alive until cancelled.
            while True:
                await asyncio.sleep(3600)
        except Exception as exc:
            logger.error("Telegram polling crashed", error=str(exc))
        finally:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as exc:
                logger.warning("Telegram shutdown error", error=str(exc))

    async def _on_stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        self._application = None
        logger.info("Telegram input stopped")

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, update: Any, context: Any) -> None:
        """python-telegram-bot MessageHandler callback."""
        try:
            event = await self._build_event(update)
        except Exception as exc:
            logger.warning("Telegram message decode failed", error=str(exc))
            return
        if event is not None:
            await self._queue.put(event)

    def _passes_filters(self, update: Any) -> bool:
        message = getattr(update, "message", None) or getattr(
            update, "effective_message", None
        )
        if message is None:
            return False
        chat = getattr(message, "chat", None)
        user = getattr(message, "from_user", None)

        if chat is None or user is None:
            return False

        if self._dm_only and getattr(chat, "type", "") != "private":
            return False

        if self._allow_chat_ids and int(chat.id) not in self._allow_chat_ids:
            return False

        if self._allow_user_ids and int(user.id) not in self._allow_user_ids:
            return False

        text = (getattr(message, "text", None) or "").strip()
        if self._command_prefix and not text.startswith(self._command_prefix):
            return False

        return True

    async def _build_event(self, update: Any) -> TriggerEvent | None:
        if not self._passes_filters(update):
            return None

        message = getattr(update, "message", None) or getattr(
            update, "effective_message", None
        )
        if message is None:
            return None

        raw_text: str = (
            getattr(message, "text", None) or getattr(message, "caption", None) or ""
        )
        text = raw_text
        if self._command_prefix and text.startswith(self._command_prefix):
            text = text[len(self._command_prefix) :].lstrip()

        parts: list[Any] = []
        if text:
            parts.append(TextPart(text=text))

        if self._include_attachments:
            image_parts = await self._extract_images(message)
            parts.extend(image_parts)

        if not parts:
            # Nothing usable — drop silently.
            return None

        chat = message.chat
        user = message.from_user
        metadata = {
            "platform": "telegram",
            "chat_id": int(chat.id),
            "user_id": int(user.id),
            "username": getattr(user, "username", None) or "",
            "message_id": int(getattr(message, "message_id", 0)),
        }

        # If only a single text part, send content as plain str so the
        # controller's normal code path is hit. Multi-part stays as list.
        if len(parts) == 1 and isinstance(parts[0], TextPart):
            content: Any = parts[0].text
        else:
            content = parts

        return TriggerEvent(
            type=EventType.USER_INPUT,
            content=content,
            context={"source": "telegram", "metadata": metadata},
        )

    async def _extract_images(self, message: Any) -> list[ImagePart]:
        """Try to pull photo attachments out of a message.

        Uses the Telegram file API to get a direct URL. On any failure
        returns an empty list — the text part still carries the message.
        """
        results: list[ImagePart] = []
        photos = getattr(message, "photo", None)
        if not photos:
            return results
        # photos is a list of PhotoSize ordered smallest → largest.
        try:
            best = photos[-1]
            tg_file = await best.get_file()
            url = getattr(tg_file, "file_path", None) or ""
            if url:
                results.append(
                    ImagePart(
                        url=url,
                        source_type="telegram_photo",
                        source_name=str(getattr(tg_file, "file_unique_id", "") or ""),
                    )
                )
        except Exception as exc:
            logger.warning("Telegram image fetch failed", error=str(exc))
        return results

    # ------------------------------------------------------------------
    # InputModule protocol
    # ------------------------------------------------------------------

    async def get_input(self) -> TriggerEvent:
        return await self._queue.get()
