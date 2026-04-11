"""Event Logger Plugin — structured JSONL log of all agent activity.

Uses pre/post hooks and callbacks to capture everything:
- pre/post_llm_call: LLM requests and responses
- pre/post_tool_execute: tool invocations and results
- pre/post_subagent_run: sub-agent lifecycle
- on_event, on_interrupt, on_task_promoted: agent events

Usage:
    plugins:
      - name: event_logger
        type: package
        module: kt_defaults.plugins.event_logger
        class: EventLoggerPlugin
        options:
          path: ./logs/events.jsonl
          include_content: false
          include_args: true
"""

import json
import time
from pathlib import Path
from typing import Any, TextIO

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


class EventLoggerPlugin(BasePlugin):
    name = "event_logger"
    priority = 1  # First to observe

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._log_path = Path(
            opts.get("path", Path.home() / ".kohakuterrarium" / "event_log.jsonl")
        )
        self._include_content = bool(opts.get("include_content", False))
        self._include_args = bool(opts.get("include_args", True))
        self._agent_name = ""
        self._file: TextIO | None = None

    def _emit(self, event_type: str, **data: Any) -> None:
        if not self._file:
            return
        record = {
            "ts": time.time(),
            "agent": self._agent_name,
            "event": event_type,
            **{k: v for k, v in data.items() if v is not None},
        }
        try:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        except Exception:
            pass

    async def on_load(self, context: PluginContext) -> None:
        self._agent_name = context.agent_name
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._log_path, "a", encoding="utf-8")
        self._emit("plugin_loaded")

    async def on_unload(self) -> None:
        self._emit("plugin_unloaded")
        if self._file:
            self._file.close()
            self._file = None

    async def on_agent_start(self) -> None:
        self._emit("agent_start")

    async def on_agent_stop(self) -> None:
        self._emit("agent_stop")

    # ── LLM hooks ──

    async def pre_llm_call(self, messages, **kwargs):
        self._emit(
            "llm_start",
            model=kwargs.get("model", ""),
            message_count=len(messages) if messages else 0,
            tool_count=len(kwargs.get("tools") or []),
        )
        return None  # Don't modify messages

    async def post_llm_call(self, messages, response, usage, **kwargs):
        data: dict[str, Any] = {
            "model": kwargs.get("model", ""),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
        }
        if self._include_content and response:
            data["response_preview"] = response[:500]
        self._emit("llm_end", **data)

    # ── Tool hooks ──

    async def pre_tool_execute(self, args, **kwargs):
        data: dict[str, Any] = {
            "tool": kwargs.get("tool_name", ""),
            "job_id": kwargs.get("job_id", ""),
        }
        if self._include_args:
            data["args_keys"] = list(args.keys()) if isinstance(args, dict) else []
        self._emit("tool_start", **data)
        return None

    async def post_tool_execute(self, result, **kwargs):
        success = getattr(result, "success", True) if result else True
        error = getattr(result, "error", None) if result else None
        self._emit(
            "tool_end",
            tool=kwargs.get("tool_name", ""),
            job_id=kwargs.get("job_id", ""),
            success=success,
            error=error[:200] if error else None,
        )
        return None

    # ── Sub-agent hooks ──

    async def pre_subagent_run(self, task, **kwargs):
        self._emit(
            "subagent_start",
            subagent=kwargs.get("name", ""),
            task_preview=task[:200] if task else "",
        )
        return None

    async def post_subagent_run(self, result, **kwargs):
        self._emit(
            "subagent_end",
            subagent=kwargs.get("name", ""),
            success=getattr(result, "success", True),
            turns=getattr(result, "turns", 0),
            total_tokens=getattr(result, "total_tokens", 0),
        )
        return None

    # ── Callbacks ──

    async def on_event(self, event=None) -> None:
        event_type = getattr(event, "type", "unknown") if event else "unknown"
        self._emit("event_received", event_type=event_type)

    async def on_interrupt(self) -> None:
        self._emit("interrupt")

    async def on_task_promoted(self, job_id="", tool_name="") -> None:
        self._emit("task_promoted", job_id=job_id, tool=tool_name)
