"""Circuit Breaker Plugin — stop retry storms on repeatedly-failing tools.

Tracks per-tool failure counts over a sliding window and opens a circuit
breaker when failures exceed a threshold. While the breaker is OPEN,
``pre_tool_dispatch`` raises :class:`PluginBlockError` so the model sees
a clear explanation instead of retrying into the same failure. Blocking
at dispatch time means the executor never spins up a task for a call
that will just be rejected.

State machine (per tool):

- ``CLOSED``   — normal; failures accumulate in a sliding deque.
- ``OPEN``     — blocked; reject all calls until ``cooldown_until``.
- ``HALF_OPEN``— one trial call allowed after cool-down expires.
  - success  → transition back to ``CLOSED`` (failures cleared).
  - failure  → back to ``OPEN`` with the cool-down doubled
    (capped at ``backoff_max_seconds``).

The plugin uses ``time.monotonic`` so tests can patch one function.
All counters live on the plugin instance for the ``PluginContext``
lifetime; nothing is persisted to the session store.

Usage::

    plugins:
      - name: circuit_breaker
        type: package
        module: kt_biome.plugins.circuit_breaker
        class: CircuitBreakerPlugin
        options:
          default:
            window_seconds: 60
            max_failures: 5
            cooldown_seconds: 30
            backoff_max_seconds: 600
          per_tool:
            bash:
              max_failures: 3
              cooldown_seconds: 60
          half_open_trial: true
          agent_names: []
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from kohakuterrarium.modules.plugin.base import (
    BasePlugin,
    PluginBlockError,
    PluginContext,
)
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


# ── State enum (plain strings; easier to serialize than IntEnum) ──

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"

_DEFAULT_WINDOW = 60.0
_DEFAULT_MAX_FAILURES = 5
_DEFAULT_COOLDOWN = 30.0
_DEFAULT_BACKOFF_MAX = 600.0
_DEFAULT_STATE_CLASSES = ("error_set", "nonzero_exit")


@dataclass
class _Settings:
    """Resolved threshold / timing settings for a specific tool."""

    window_seconds: float = _DEFAULT_WINDOW
    max_failures: int = _DEFAULT_MAX_FAILURES
    cooldown_seconds: float = _DEFAULT_COOLDOWN
    backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX


@dataclass
class _BreakerState:
    """Mutable per-tool breaker state."""

    state: str = STATE_CLOSED
    failures: deque = field(default_factory=deque)  # timestamps (monotonic)
    last_failure_ts: float | None = None
    cooldown_until: float = 0.0
    # How long the NEXT OPEN should last. Doubles every repeated OPEN,
    # resets back to the configured base after a CLOSED success.
    current_cooldown: float = 0.0
    open_count: int = 0  # Number of consecutive OPEN transitions.


# ── Plugin ──


class CircuitBreakerPlugin(BasePlugin):
    """Blocks tool calls after repeated failures; cools down with backoff."""

    name = "circuit_breaker"
    priority = 15  # Runs after auth/guard plugins, before heavy-weight ones.
    description = "Circuit breaker for repeatedly-failing tools."

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        opts = options or {}
        self._enabled: bool = bool(opts.get("enabled", True))
        self._half_open_trial: bool = bool(opts.get("half_open_trial", True))
        self._agent_names: list[str] = list(opts.get("agent_names") or [])
        self._state_classes: tuple[str, ...] = tuple(
            opts.get("state_classes") or _DEFAULT_STATE_CLASSES
        )

        default = opts.get("default") or {}
        self._default = _Settings(
            window_seconds=float(default.get("window_seconds", _DEFAULT_WINDOW)),
            max_failures=int(default.get("max_failures", _DEFAULT_MAX_FAILURES)),
            cooldown_seconds=float(default.get("cooldown_seconds", _DEFAULT_COOLDOWN)),
            backoff_max_seconds=float(
                default.get("backoff_max_seconds", _DEFAULT_BACKOFF_MAX)
            ),
        )

        self._per_tool: dict[str, _Settings] = {}
        for tool_name, tool_opts in (opts.get("per_tool") or {}).items():
            self._per_tool[tool_name] = _Settings(
                window_seconds=float(
                    tool_opts.get("window_seconds", self._default.window_seconds)
                ),
                max_failures=int(
                    tool_opts.get("max_failures", self._default.max_failures)
                ),
                cooldown_seconds=float(
                    tool_opts.get("cooldown_seconds", self._default.cooldown_seconds)
                ),
                backoff_max_seconds=float(
                    tool_opts.get(
                        "backoff_max_seconds", self._default.backoff_max_seconds
                    )
                ),
            )

        self._breakers: dict[str, _BreakerState] = {}
        self._ctx: PluginContext | None = None

    # ── Time source (overridable in tests) ──

    def _now(self) -> float:
        """Return the current monotonic time. Overridable in tests."""
        return time.monotonic()

    # ── Lifecycle ──

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context

    # ── Scoping ──

    def should_apply(self, context: PluginContext | None = None) -> bool:
        """Return True if this plugin should act for the given agent."""
        if not self._enabled:
            return False
        if not self._agent_names:
            return True
        ctx = context or self._ctx
        if ctx is None:
            return True
        return ctx.agent_name in self._agent_names

    # ── Internal helpers ──

    def _settings_for(self, tool_name: str) -> _Settings:
        return self._per_tool.get(tool_name, self._default)

    def _get_breaker(self, tool_name: str) -> _BreakerState:
        br = self._breakers.get(tool_name)
        if br is None:
            br = _BreakerState()
            br.current_cooldown = self._settings_for(tool_name).cooldown_seconds
            self._breakers[tool_name] = br
        return br

    def _prune_old(self, br: _BreakerState, window: float, now: float) -> None:
        cutoff = now - window
        while br.failures and br.failures[0] < cutoff:
            br.failures.popleft()

    def _is_failure(self, result: Any) -> bool:
        """Decide whether a post-tool result counts as a failure."""
        if result is None:
            return False
        if isinstance(result, BaseException):
            return True
        classes = self._state_classes
        if "error_set" in classes:
            err = getattr(result, "error", None)
            if err:
                return True
        if "nonzero_exit" in classes:
            exit_code = getattr(result, "exit_code", None)
            if exit_code is not None and exit_code != 0:
                return True
        return False

    def _open_breaker(self, tool_name: str, br: _BreakerState, now: float) -> None:
        settings = self._settings_for(tool_name)
        # First time opening uses the configured cooldown; subsequent opens
        # double the previous cooldown (capped).
        if br.open_count == 0 or br.current_cooldown <= 0:
            br.current_cooldown = settings.cooldown_seconds
        else:
            br.current_cooldown = min(
                br.current_cooldown * 2, settings.backoff_max_seconds
            )
        br.open_count += 1
        br.cooldown_until = now + br.current_cooldown
        prev_state = br.state
        br.state = STATE_OPEN
        logger.info(
            "Circuit breaker opened",
            tool=tool_name,
            failures=len(br.failures),
            cooldown_seconds=br.current_cooldown,
            prev_state=prev_state,
            open_count=br.open_count,
        )

    def _close_breaker(self, tool_name: str, br: _BreakerState) -> None:
        settings = self._settings_for(tool_name)
        prev_state = br.state
        br.state = STATE_CLOSED
        br.failures.clear()
        br.cooldown_until = 0.0
        br.open_count = 0
        br.current_cooldown = settings.cooldown_seconds
        logger.info(
            "Circuit breaker closed",
            tool=tool_name,
            prev_state=prev_state,
        )

    def _half_open(self, tool_name: str, br: _BreakerState) -> None:
        prev_state = br.state
        br.state = STATE_HALF_OPEN
        logger.info(
            "Circuit breaker half-open trial",
            tool=tool_name,
            prev_state=prev_state,
        )

    # ── Hooks ──

    async def pre_tool_dispatch(self, call: Any, context: PluginContext) -> Any | None:
        """Reject the call if the breaker for the tool is OPEN.

        Fires before the executor submits the call. If the cool-down
        has expired, transition to HALF_OPEN and allow a single trial.
        Otherwise raise :class:`PluginBlockError` — the error text
        becomes the tool result.
        """
        if not self.should_apply(context):
            return None
        tool_name = getattr(call, "name", "") or ""
        if not tool_name:
            return None
        br = self._breakers.get(tool_name)
        if br is None:
            return None
        if br.state == STATE_CLOSED:
            return None

        now = self._now()
        if br.state == STATE_OPEN:
            if now >= br.cooldown_until and self._half_open_trial:
                self._half_open(tool_name, br)
                return None
            if now >= br.cooldown_until and not self._half_open_trial:
                # Without half-open trials, an expired cooldown just closes
                # the breaker and lets the call through.
                self._close_breaker(tool_name, br)
                return None
            remaining = max(0.0, br.cooldown_until - now)
            raise PluginBlockError(
                (
                    f"circuit breaker open: tool {tool_name!r} has failed "
                    f"{len(br.failures)} times in "
                    f"{self._settings_for(tool_name).window_seconds:g}s; "
                    f"cool-down for {remaining:.1f}s more"
                )
            )
        # HALF_OPEN: allow exactly this one call to proceed.
        return None

    async def post_tool_execute(self, result: Any, **kwargs: Any) -> Any | None:
        """Observe the result — never raise, never modify."""
        if not self.should_apply():
            return None
        tool_name = kwargs.get("tool_name", "")
        if not tool_name:
            return None
        settings = self._settings_for(tool_name)
        now = self._now()

        failed = self._is_failure(result)
        br = self._get_breaker(tool_name)

        if br.state == STATE_HALF_OPEN:
            if failed:
                # Trial failed — re-open with doubled cool-down.
                br.failures.append(now)
                br.last_failure_ts = now
                self._open_breaker(tool_name, br, now)
            else:
                self._close_breaker(tool_name, br)
            return None

        # CLOSED or OPEN (a racing post-hook from a pre-block pass-through):
        if failed:
            br.failures.append(now)
            br.last_failure_ts = now
            self._prune_old(br, settings.window_seconds, now)
            if br.state == STATE_CLOSED and len(br.failures) >= settings.max_failures:
                self._open_breaker(tool_name, br, now)
        else:
            # Success in CLOSED state — decay failures back toward empty.
            # A single success clears the window; this matches the
            # conventional "consecutive failures" intuition while the
            # sliding-window still protects against false opens after
            # long idle.
            if br.state == STATE_CLOSED and br.failures:
                br.failures.clear()
        return None

    # ── Public admin API ──

    def reset(self, tool_name: str | None = None) -> None:
        """Clear breaker state for *tool_name* (or all tools if None)."""
        if tool_name is None:
            self._breakers.clear()
            logger.info("Circuit breakers reset (all tools)")
            return
        if tool_name in self._breakers:
            del self._breakers[tool_name]
            logger.info("Circuit breaker reset", tool=tool_name)

    def get_state(self) -> dict[str, dict[str, Any]]:
        """Snapshot of per-tool breaker state (for admin commands)."""
        now = self._now()
        out: dict[str, dict[str, Any]] = {}
        for tool_name, br in self._breakers.items():
            settings = self._settings_for(tool_name)
            # Don't mutate in a read — compute a pruned count locally.
            cutoff = now - settings.window_seconds
            live = sum(1 for ts in br.failures if ts >= cutoff)
            remaining = (
                max(0.0, br.cooldown_until - now) if br.state == STATE_OPEN else 0.0
            )
            out[tool_name] = {
                "count": live,
                "total_failures": len(br.failures),
                "last_failure_ts": br.last_failure_ts,
                "state": br.state,
                "cooldown_remaining": remaining,
                "open_count": br.open_count,
                "current_cooldown": br.current_cooldown,
            }
        return out
