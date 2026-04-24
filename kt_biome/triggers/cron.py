"""Cron Trigger — fires on a cron schedule.

Adds full cron-expression support to kt-biome. The built-in
``SchedulerTrigger`` (``kohakuterrarium.modules.trigger.scheduler``)
only handles ``every_minutes`` / ``daily_at`` / ``hourly_at``
shortcuts, so this is a NEW implementation, not a wrapper.

Parser strategy:
    * If ``croniter`` is importable we use it — it handles every edge
      case (named months / weekdays, ``L``, ``#`` specifiers, DST, the
      6-field seconds form).
    * Otherwise we fall back to a small built-in parser covering the
      common subset: ``*``, ``N``, ``N-M``, ``*/N``, and comma lists on
      each of the five standard fields (minute hour dom month dow).
      Named weekdays / months and 6-field forms require ``croniter``.

YAML example — see ``cron.manifest.yaml`` for the full schema.
"""

import asyncio
from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kohakuterrarium.core.events import EventType, TriggerEvent
from kohakuterrarium.modules.trigger.base import BaseTrigger
from kohakuterrarium.utils.logging import get_logger

try:  # Optional dependency — we degrade gracefully if missing.
    from croniter import croniter as _croniter  # type: ignore[import-not-found]

    _HAS_CRONITER = True
except ImportError:  # pragma: no cover - exercised without croniter installed
    _croniter = None  # type: ignore[assignment]
    _HAS_CRONITER = False

logger = get_logger(__name__)

_BACKFILL_CHOICES = ("skip_missed", "run_once_if_missed")

# Field ranges for the built-in parser: (min, max).
_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week (0 = Sunday)
)


class CronExpressionError(ValueError):
    """Raised when a cron expression or trigger option is invalid."""


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    """Parse a single cron field into the set of matching integers.

    Supports ``*``, ``N``, ``N-M``, ``*/N``, and comma lists.
    """
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronExpressionError(f"Empty sub-expression in field: {spec!r}")

        step = 1
        if "/" in part:
            range_part, step_part = part.split("/", 1)
            try:
                step = int(step_part)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid step in {part!r}") from exc
            if step <= 0:
                raise CronExpressionError(f"Step must be >= 1 in {part!r}")
        else:
            range_part = part

        if range_part == "*":
            start, end = lo, hi
        elif "-" in range_part:
            start_s, end_s = range_part.split("-", 1)
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid range in {part!r}") from exc
        else:
            try:
                start = end = int(range_part)
            except ValueError as exc:
                raise CronExpressionError(
                    f"Invalid value in {part!r} (named weekdays/months "
                    "require the croniter package)"
                ) from exc

        if start < lo or end > hi or start > end:
            raise CronExpressionError(
                f"Value out of range in {part!r}: expected [{lo}, {hi}]"
            )
        values.update(range(start, end + 1, step))
    return values


class _BuiltinCron:
    """Minimal cron evaluator for the standard 5-field common subset."""

    __slots__ = ("_fields", "_expression")

    def __init__(self, expression: str) -> None:
        self._expression = expression
        tokens = expression.split()
        if len(tokens) != 5:
            raise CronExpressionError(
                "Built-in parser only supports 5-field cron; got "
                f"{len(tokens)} fields. Install croniter for extended forms."
            )
        self._fields = tuple(
            _parse_field(token, lo, hi)
            for token, (lo, hi) in zip(tokens, _FIELD_RANGES, strict=True)
        )

    def _matches(self, dt: datetime) -> bool:
        minute, hour, dom, month, dow = self._fields
        # Cron DOW: 0=Sun..6=Sat. Python weekday(): 0=Mon..6=Sun -> convert.
        weekday = (dt.weekday() + 1) % 7
        if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
            return False
        # Classic cron: if BOTH dom and dow are restricted, match EITHER.
        dom_restricted = dom != set(range(1, 32))
        dow_restricted = dow != set(range(0, 7))
        dom_ok, dow_ok = dt.day in dom, weekday in dow
        if dom_restricted and dow_restricted:
            return dom_ok or dow_ok
        if dom_restricted:
            return dom_ok
        if dow_restricted:
            return dow_ok
        return True

    def next_after(self, start: datetime) -> datetime:
        """Return the next firing time strictly after ``start``."""
        candidate = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
        # 4 years in minutes — bounded so bad expressions can't hang.
        for _ in range(366 * 4 * 24 * 60):
            if self._matches(candidate):
                return candidate
            candidate = candidate + timedelta(minutes=1)
        raise CronExpressionError(
            f"No firing time found within 4 years for: {self._expression!r}"
        )

    def prev_before(self, start: datetime) -> datetime | None:
        """Return the most recent firing time strictly before ``start``."""
        candidate = (start - timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(60 * 24 * 7):  # look back up to a week
            if self._matches(candidate):
                return candidate
            candidate -= timedelta(minutes=1)
        return None


class CronTrigger(BaseTrigger):
    """Fires per a cron expression with TZ-aware scheduling."""

    resumable = True
    universal = False  # Config-driven; not agent-installable via tool.

    def __init__(
        self,
        expression: str = "* * * * *",
        timezone: str = "UTC",
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        backfill: str = "skip_missed",
        enabled: bool = True,
        prompt: str | None = None,
        **options: Any,
    ) -> None:
        # ``content`` is the canonical field name per the YAML schema;
        # ``prompt`` is an alias for BaseTrigger consistency.
        super().__init__(prompt=prompt or content, **options)
        self.expression = expression
        self.timezone_name = timezone
        self.content = content or prompt or ""
        self.metadata: dict[str, Any] = dict(metadata or {})
        if backfill not in _BACKFILL_CHOICES:
            raise CronExpressionError(
                f"Invalid backfill policy {backfill!r}; "
                f"expected one of {_BACKFILL_CHOICES}"
            )
        self.backfill = backfill
        self.enabled = enabled

        try:
            self._tz: tzinfo = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise CronExpressionError(f"Unknown timezone {timezone!r}") from exc

        # Validate eagerly so config errors surface at construction.
        self._validate_expression()

        self._stop_event: asyncio.Event | None = None
        self._started_at: datetime | None = None
        self._pending_backfill_fire = False

    def available(self) -> bool:
        """Always functional thanks to the built-in fallback parser."""
        return True

    @classmethod
    def has_full_cron_support(cls) -> bool:
        """True if ``croniter`` is installed (enables 6-field / named forms)."""
        return _HAS_CRONITER

    # ------------------------------------------------------------------
    # BaseTrigger hooks
    # ------------------------------------------------------------------

    async def _on_start(self) -> None:
        if not self.enabled:
            logger.info("Cron trigger disabled; will not fire", expr=self.expression)
        self._stop_event = asyncio.Event()
        now = self._now()
        self._started_at = now
        if self.backfill == "run_once_if_missed":
            prev = self._compute_prev(now)
            if prev is not None and prev < now:
                self._pending_backfill_fire = True
                logger.info(
                    "Cron backfill: firing once for missed slot",
                    expr=self.expression,
                    missed_slot=prev.isoformat(),
                )
        logger.debug(
            "Cron trigger started",
            expr=self.expression,
            tz=self.timezone_name,
            backfill=self.backfill,
        )

    async def _on_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def wait_for_trigger(self) -> TriggerEvent | None:
        if not self._running or not self.enabled or self._stop_event is None:
            # Disabled: block forever so the manager loop doesn't spin.
            if self._stop_event is not None:
                await self._stop_event.wait()
            return None

        if self._pending_backfill_fire:
            self._pending_backfill_fire = False
            return self._fire(self._now(), backfill=True)

        now = self._now()
        next_fire = self._compute_next(now)
        # Safety floor: never pass a non-positive timeout to wait_for.
        wait_seconds = max((next_fire - now).total_seconds(), 1.0)
        logger.info(
            "Cron trigger scheduled next fire",
            expr=self.expression,
            next_fire=next_fire.isoformat(),
            wait_s=round(wait_seconds, 3),
        )
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
            return None  # Stopped while sleeping.
        except asyncio.TimeoutError:
            pass  # CancelledError propagates naturally.
        if not self._running:
            return None
        return self._fire(self._now(), backfill=False)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_resume_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "timezone": self.timezone_name,
            "content": self.content,
            "metadata": dict(self.metadata),
            "backfill": self.backfill,
            "enabled": self.enabled,
        }

    @classmethod
    def from_resume_dict(cls, data: dict[str, Any]) -> "CronTrigger":
        return cls(
            expression=data.get("expression", "* * * * *"),
            timezone=data.get("timezone", "UTC"),
            content=data.get("content"),
            metadata=data.get("metadata") or {},
            backfill=data.get("backfill", "skip_missed"),
            enabled=data.get("enabled", True),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(tz=self._tz)

    def _validate_expression(self) -> None:
        try:
            self._compute_next(datetime.now(tz=self._tz))
        except Exception as exc:
            raise CronExpressionError(
                f"Invalid cron expression {self.expression!r}: {exc}"
            ) from exc

    def _compute_next(self, start: datetime) -> datetime:
        if _HAS_CRONITER:
            nxt = _croniter(self.expression, start).get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=self._tz)
            return nxt
        return _BuiltinCron(self.expression).next_after(start)

    def _compute_prev(self, start: datetime) -> datetime | None:
        if _HAS_CRONITER:
            prv = _croniter(self.expression, start).get_prev(datetime)
            if prv.tzinfo is None:
                prv = prv.replace(tzinfo=self._tz)
            return prv
        return _BuiltinCron(self.expression).prev_before(start)

    def _fire(self, now: datetime, backfill: bool) -> TriggerEvent:
        context: dict[str, Any] = {
            "trigger": "cron",
            "expression": self.expression,
            "timezone": self.timezone_name,
            "fired_at": now.isoformat(),
            "backfill": backfill,
        }
        # Merge user metadata without shadowing trigger-owned keys.
        for key, value in self.metadata.items():
            context.setdefault(key, value)
        return self._create_event(
            EventType.TIMER,
            content=self.content or self.prompt or f"cron fire @ {now.isoformat()}",
            context=context,
        )
