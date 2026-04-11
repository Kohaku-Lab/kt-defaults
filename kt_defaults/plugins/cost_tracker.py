"""Cost Tracker Plugin — track LLM token usage and estimated cost.

Uses post_llm_call hook to observe every LLM response. Accumulates
token counts and estimated cost. Persists to session state.

Usage:
    plugins:
      - name: cost_tracker
        type: package
        module: kt_defaults.plugins.cost_tracker
        class: CostTrackerPlugin
        options:
          budget_usd: 5.0
          warn_at: 0.8
"""

import time
from typing import Any

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.4": {"input": 2.50, "output": 10.00},
    "gpt-5.4-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "gemini-3.1-pro": {"input": 1.25, "output": 10.00},
    "gemini-3-flash": {"input": 0.15, "output": 0.60},
    "_default": {"input": 1.00, "output": 5.00},
}


class CostTrackerPlugin(BasePlugin):
    name = "cost_tracker"
    priority = 10

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._budget = float(opts.get("budget_usd", 0))
        self._warn_at = float(opts.get("warn_at", 0.8))
        self._pricing = {**_DEFAULT_PRICING, **opts.get("pricing", {})}
        self._total_cost = 0.0
        self._total_input = 0
        self._total_output = 0
        self._total_cached = 0
        self._call_count = 0
        self._warned = False
        self._start_time = 0.0
        self._ctx: PluginContext | None = None

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context
        self._start_time = time.time()
        saved = context.get_state("total_cost")
        if saved is not None:
            self._total_cost = float(saved)
            self._total_input = int(context.get_state("total_input") or 0)
            self._total_output = int(context.get_state("total_output") or 0)
            self._call_count = int(context.get_state("call_count") or 0)

    async def post_llm_call(self, messages, response, usage, **kwargs):
        """Observe LLM response and track cost."""
        model = kwargs.get("model", "")
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        cached = usage.get("cached_tokens", 0)

        self._total_input += prompt
        self._total_output += completion
        self._total_cached += cached
        self._call_count += 1

        short = model.split("/")[-1] if "/" in model else model
        pricing = self._pricing.get(short, self._pricing["_default"])
        effective = max(0, prompt - cached)
        cost = (
            effective * pricing["input"] + completion * pricing["output"]
        ) / 1_000_000
        self._total_cost += cost

        if self._ctx:
            self._ctx.set_state("total_cost", self._total_cost)
            self._ctx.set_state("total_input", self._total_input)
            self._ctx.set_state("total_output", self._total_output)
            self._ctx.set_state("call_count", self._call_count)

        if self._budget > 0:
            pct = self._total_cost / self._budget
            if pct >= 1.0:
                logger.warning("Budget exhausted", cost=f"${self._total_cost:.4f}")
            elif pct >= self._warn_at and not self._warned:
                self._warned = True
                logger.warning("Budget warning", cost=f"${self._total_cost:.4f}")

    async def on_agent_stop(self) -> None:
        elapsed = time.time() - self._start_time if self._start_time else 0
        logger.info(
            "Session cost summary",
            total_cost=f"${self._total_cost:.4f}",
            calls=self._call_count,
            input_tokens=self._total_input,
            output_tokens=self._total_output,
            runtime=f"{int(elapsed // 60)}m",
        )
