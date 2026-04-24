"""Pre-Tool Checkpoint Plugin — auto-snapshot workspace before destructive tools.

Takes a ``git stash`` snapshot in the agent's cwd before destructive tools
(``write``, ``edit``, ``multi_edit``, or ``bash`` with dangerous commands)
actually run, so the user can revert. Proposal reference: ``plans/harness/
proposal.md`` §4.3 (H4 in the master table).

Design principles
-----------------
* **Never raises.** All failures are swallowed — checkpointing is a
  best-effort safety net that must not block the agent.
* **Silent when git is not available.** Missing ``git`` binary, non-repo
  cwd, and dirty-state edge cases are logged at DEBUG, not WARNING.
* **Cheap to run.** ``git stash push --keep-index -u`` with a 5 s timeout.
  No shell, no pipes, no repo scan.

The checkpoint log lives on the Session scratchpad under key
``pev_checkpoint_log``. Each entry is a dict:

    {
        "tool": "write",
        "timestamp": "2026-04-23T12:34:56Z",
        "stash_ref": "stash@{0}",
        "message": "kt-checkpoint write@2026-04-23T12:34:56Z",
        "cwd": "C:/.../agent_work",
    }

A future ``/revert`` slash command can read the log via
:meth:`CheckpointPlugin.list_checkpoints` and invoke ``git stash pop``.

Usage in ``config.yaml``::

    plugins:
      - name: checkpoint
        type: package
        module: kt_biome.plugins.checkpoint
        class: CheckpointPlugin
        options:
          backend: git
          tools_to_checkpoint: [write, edit, multi_edit]
          bash_destructive_patterns:
            - "\\\\brm\\\\s+-[rRf]"
            - "\\\\bgit\\\\s+reset\\\\s+--hard"
"""

import datetime
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


SCRATCHPAD_KEY = "pev_checkpoint_log"

_DEFAULT_TOOLS: list[str] = ["write", "edit", "multi_edit"]

_DEFAULT_BASH_PATTERNS: list[str] = [
    r"\brm\s+-[rRf]",
    r"\bgit\s+reset\s+--hard",
    r"\bgit\s+clean\s+-[fdx]",
    r"\bdropdb\b",
    r"\bmkfs\.",
]

_DEFAULT_MESSAGE_TEMPLATE = "kt-checkpoint {tool}@{timestamp}"

_SUBPROCESS_TIMEOUT = 5.0


class CheckpointPlugin(BasePlugin):
    """Snapshot the workspace via ``git stash`` before destructive tools."""

    name = "checkpoint"
    priority = 15  # Run before most plugins so snapshot is taken first.

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        opts = options or {}

        self._enabled: bool = bool(opts.get("enabled", True))
        self._backend: str = str(opts.get("backend", "git"))
        self._tools: set[str] = {
            str(t) for t in opts.get("tools_to_checkpoint", _DEFAULT_TOOLS)
        }
        self._message_template: str = str(
            opts.get("message_template", _DEFAULT_MESSAGE_TEMPLATE)
        )
        self._agent_names: list[str] = [
            str(a) for a in opts.get("agent_names", []) or []
        ]
        self._max_history: int = int(opts.get("max_history", 50))

        patterns_raw = opts.get("bash_destructive_patterns", _DEFAULT_BASH_PATTERNS)
        self._bash_patterns: list[re.Pattern[str]] = []
        for raw in patterns_raw:
            try:
                self._bash_patterns.append(re.compile(str(raw)))
            except re.error as exc:
                logger.warning(
                    "Invalid checkpoint bash pattern; ignored",
                    pattern=str(raw),
                    error=str(exc),
                )

        self._ctx: PluginContext | None = None
        self._git_available: bool = shutil.which("git") is not None

    # ────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context
        if not self._git_available and self._backend == "git":
            logger.debug(
                "Checkpoint plugin loaded but git not found on PATH; "
                "plugin will no-op",
                agent=context.agent_name,
            )
            return
        logger.debug(
            "Checkpoint plugin loaded",
            agent=context.agent_name,
            backend=self._backend,
            tools=sorted(self._tools),
        )

    # ────────────────────────────────────────────────────────────
    # Hook
    # ────────────────────────────────────────────────────────────

    async def pre_tool_dispatch(self, call: Any, context: PluginContext) -> Any | None:
        """Take a checkpoint if the tool is on the deny-list.

        Runs before the executor dispatches the call so the snapshot
        happens as early as possible. Always returns ``None`` — we never
        modify the tool call and never block it even if checkpointing
        fails.
        """
        if not self._enabled or self._backend == "disabled":
            return None

        tool_name = getattr(call, "name", "") or ""
        if not tool_name:
            return None

        if self._agent_names:
            agent = context.agent_name if context else ""
            if agent and agent not in self._agent_names:
                return None

        args = getattr(call, "args", {}) or {}
        if not self._should_checkpoint(tool_name, args):
            return None

        try:
            self._take_checkpoint(tool_name)
        except Exception as exc:
            # Blanket safety: this plugin MUST NOT raise.
            logger.debug(
                "Checkpoint attempt raised; swallowed to keep tool running",
                tool=tool_name,
                error=str(exc),
            )
        return None

    # ────────────────────────────────────────────────────────────
    # Decision
    # ────────────────────────────────────────────────────────────

    def _should_checkpoint(self, tool_name: str, args: dict | None) -> bool:
        """Decide whether to snapshot for this tool invocation."""
        if tool_name in self._tools:
            return True
        if tool_name == "bash":
            command = ""
            if isinstance(args, dict):
                command = str(args.get("command") or args.get("cmd") or "")
            if not command:
                return False
            for pat in self._bash_patterns:
                if pat.search(command):
                    return True
        return False

    # ────────────────────────────────────────────────────────────
    # Git backend
    # ────────────────────────────────────────────────────────────

    def _take_checkpoint(self, tool_name: str) -> None:
        """Run the backend snapshot and record it to scratchpad."""
        cwd = self._agent_cwd()
        if cwd is None:
            logger.debug("Checkpoint skipped: no working dir available")
            return

        if self._backend != "git":
            logger.debug(
                "Checkpoint backend not implemented; skipped",
                backend=self._backend,
            )
            return

        if not self._git_available:
            return

        if not self._is_git_repo(cwd):
            logger.debug("Checkpoint skipped: cwd is not a git repo", cwd=str(cwd))
            return

        timestamp = _iso_timestamp()
        # Sanitise the message so an exotic template can't break the
        # git CLI on Windows (newlines, CRs, NULs).
        message = _sanitise_message(
            self._message_template.format(tool=tool_name, timestamp=timestamp)
        )

        try:
            result = subprocess.run(
                [
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "--keep-index",
                    "--message",
                    message,
                ],
                cwd=str(cwd),
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("git stash push failed", tool=tool_name, error=str(exc))
            return

        if result.returncode != 0:
            logger.debug(
                "git stash push returned non-zero",
                tool=tool_name,
                code=result.returncode,
                stderr=(result.stderr or "").strip()[:200],
            )
            return

        stdout = (result.stdout or "").strip()
        # "No local changes to save" → git exits 0 but no stash created.
        if "no local changes" in stdout.lower() or not stdout:
            logger.debug(
                "Checkpoint: nothing to snapshot; skipping log entry",
                tool=tool_name,
            )
            return

        stash_ref = self._newest_stash_ref(cwd)
        entry: dict[str, Any] = {
            "tool": tool_name,
            "timestamp": timestamp,
            "stash_ref": stash_ref or "stash@{0}",
            "message": message,
            "cwd": str(cwd),
        }
        self._append_log_entry(entry)
        logger.info(
            "Checkpoint created",
            tool=tool_name,
            stash_ref=entry["stash_ref"],
            cwd=str(cwd),
        )

    def _is_git_repo(self, cwd: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(cwd),
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("git rev-parse failed", cwd=str(cwd), error=str(exc))
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _newest_stash_ref(self, cwd: Path) -> str | None:
        """Return ``stash@{0}`` if the stash list is non-empty."""
        try:
            result = subprocess.run(
                ["git", "stash", "list", "-n", "1"],
                cwd=str(cwd),
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        first_line = (result.stdout or "").splitlines()[:1]
        if not first_line:
            return None
        ref = first_line[0].split(":", 1)[0].strip()
        return ref or None

    # ────────────────────────────────────────────────────────────
    # Scratchpad log
    # ────────────────────────────────────────────────────────────

    def _append_log_entry(self, entry: dict[str, Any]) -> None:
        """Persist a checkpoint entry to the session scratchpad."""
        scratchpad = self._scratchpad()
        if scratchpad is None:
            return

        existing = _decode_log(scratchpad.get(SCRATCHPAD_KEY))
        existing.append(entry)
        if self._max_history > 0 and len(existing) > self._max_history:
            existing = existing[-self._max_history :]
        try:
            scratchpad.set(SCRATCHPAD_KEY, json.dumps(existing, ensure_ascii=False))
        except Exception as exc:
            logger.debug("Failed to persist checkpoint log", error=str(exc))

    def _scratchpad(self) -> Any | None:
        if self._ctx is None:
            return None
        scratchpad = self._ctx.scratchpad
        if scratchpad is not None:
            return scratchpad
        # Fallback: some fake agents used in tests expose scratchpad only
        # via ``agent.session.scratchpad``. The public ``context.scratchpad``
        # covers the canonical path; this handles the legacy shape.
        host = self._ctx.host_agent
        if host is None:
            return None
        session = getattr(host, "session", None)
        return getattr(session, "scratchpad", None) if session else None

    def _agent_cwd(self) -> Path | None:
        if not self._ctx:
            return None
        cwd = self._ctx.working_dir
        if cwd is None:
            return None
        try:
            path = Path(cwd)
        except TypeError:
            return None
        if not path.exists():
            return None
        return path

    # ────────────────────────────────────────────────────────────
    # Public helpers
    # ────────────────────────────────────────────────────────────

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """Return the in-session checkpoint log (most recent last)."""
        scratchpad = self._scratchpad()
        if scratchpad is None:
            return []
        return _decode_log(scratchpad.get(SCRATCHPAD_KEY))

    @classmethod
    def list_checkpoints_for_session(cls, session: Any) -> list[dict[str, Any]]:
        """Read the checkpoint log off an arbitrary ``Session``.

        Intended for a future ``/revert`` slash command that has the
        session handle but not necessarily the plugin instance.
        """
        scratchpad = getattr(session, "scratchpad", None)
        if scratchpad is None:
            return []
        return _decode_log(scratchpad.get(SCRATCHPAD_KEY))

    def info(self) -> dict[str, Any]:
        """Summarise plugin state for operator inspection."""
        return {
            "enabled": self._enabled,
            "backend": self._backend,
            "git_available": self._git_available,
            "tools": sorted(self._tools),
            "bash_patterns": [p.pattern for p in self._bash_patterns],
            "checkpoints": self.list_checkpoints(),
        }


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────


def _iso_timestamp() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sanitise_message(message: str) -> str:
    """Strip CR/LF/NUL so git doesn't reject the stash message on Windows."""
    return (
        message.replace("\r", " ").replace("\n", " ").replace("\x00", " ").strip()
        or "kt-checkpoint"
    )


def _decode_log(raw: str | None) -> list[dict[str, Any]]:
    """Decode a JSON-encoded scratchpad entry into a list of dicts."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]
