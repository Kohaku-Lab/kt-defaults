"""Context Files Loader Plugin — walk cwd up for AGENTS.md / .cursorrules / ...

Implements proposal §4.4. A per-turn reload alternative to
``AgentConfig.prompt_context_files`` (which is frozen at system-prompt
build time). Walks from a configured starting directory up to a stop
anchor (default: nearest git root), reads any matching context files,
scans them for prompt-injection patterns, and injects the surviving
content into the outgoing message list.

Design choices:
  - Regex list compiled once at init; invalid user-supplied patterns
    are logged and skipped (never raised).
  - File cache keyed by ``(path, mtime)``; invalidated on mtime change.
  - Hook never raises. Any unexpected error logs and returns the
    original messages untouched.
  - Sentinel line in the injected preamble prevents double injection
    when plugins run against messages that already contain our block.

Usage in config.yaml::

    plugins:
      - name: context_files
        module: kt_biome.plugins.context_files
        class: ContextFilesPlugin
        options:
          files:
            - AGENTS.md
            - CLAUDE.md
            - .kt/context.md
            - .cursorrules
            - .hermes.md
          walk_from: cwd          # cwd | agent_path | <fixed path>
          stop_at: git_root       # git_root | filesystem_root | <fixed path>
          max_total_bytes: 32768
          max_per_file_bytes: 16384
          injection_patterns: []  # list of regex strings (overrides defaults)
          injection_action: block # block | annotate
          position: after_system  # after_system | prepend_last_user
          preamble: "Repository context files loaded by KohakuTerrarium:"
          agent_names: []
          reload_per_turn: true
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────

DEFAULT_FILES: list[str] = [
    "AGENTS.md",
    "CLAUDE.md",
    ".kt/context.md",
    ".cursorrules",
    ".hermes.md",
]

# Patterns adapted from Hermes-agent `_CONTEXT_THREAT_PATTERNS` plus a
# few conservative additions. Keep the list small and high-signal.
DEFAULT_INJECTION_PATTERNS: list[str] = [
    r"(?i)ignore (?:all|any|the) (?:previous|above|prior|preceding) (?:instructions?|prompts?|messages?)",
    r"(?i)disregard (?:all|any|the) (?:previous|above|prior) (?:instructions?|prompts?)",
    r"(?i)you are now (?:a|an|the) ",
    r"(?i)pretend (?:to be|you are) ",
    r"(?i)^\s*system\s*:\s*",
    r"(?i)<\|(?:im_start|im_end|system|assistant|user)\|>",
    r"(?i)\[INST\]|\[/INST\]",
    r"<!--\s*(?:prompt|system|inject)[^>]*-->",
    r"(?i)reveal (?:your|the) (?:system )?prompt",
    r"(?i)(?:curl|wget)\s+[^\s]*(?:\?|&)(?:data|q|key|token)=",
]

SENTINEL = "<!-- kt-context-files -->"


# ── Options ──────────────────────────────────────────────────────────


@dataclass
class _Options:
    enabled: bool = True
    files: list[str] = field(default_factory=list)
    walk_from: str = "cwd"
    stop_at: str = "git_root"
    max_total_bytes: int = 32768
    max_per_file_bytes: int = 16384
    injection_patterns: list[str] | None = None
    injection_action: str = "block"
    position: str = "after_system"
    preamble: str = "Repository context files loaded by KohakuTerrarium:"
    agent_names: list[str] = field(default_factory=list)
    reload_per_turn: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "_Options":
        opts = raw or {}
        return cls(
            enabled=bool(opts.get("enabled", True)),
            files=list(opts.get("files") or DEFAULT_FILES),
            walk_from=str(opts.get("walk_from", "cwd")),
            stop_at=str(opts.get("stop_at", "git_root")),
            max_total_bytes=int(opts.get("max_total_bytes", 32768)),
            max_per_file_bytes=int(opts.get("max_per_file_bytes", 16384)),
            injection_patterns=opts.get("injection_patterns"),
            injection_action=str(opts.get("injection_action", "block")),
            position=str(opts.get("position", "after_system")),
            preamble=str(
                opts.get(
                    "preamble", "Repository context files loaded by KohakuTerrarium:"
                )
            ),
            agent_names=list(opts.get("agent_names") or []),
            reload_per_turn=bool(opts.get("reload_per_turn", True)),
        )


# ── Cache entry ──────────────────────────────────────────────────────


@dataclass
class _CachedRead:
    mtime: float
    size: int
    content: str
    redacted: bool
    patterns_hit: list[str]


# ── Plugin ───────────────────────────────────────────────────────────


class ContextFilesPlugin(BasePlugin):
    """Walk for and inject AGENTS.md-style context files each turn."""

    name = "context_files"
    priority = 25  # Before memory plugins (45) so context is visible.
    description = (
        "Walk cwd up to git root for AGENTS.md / .cursorrules / .hermes.md, "
        "scan for prompt injection, and inject into every LLM call."
    )

    def __init__(self, options: dict[str, Any] | None = None):
        self._opts = _Options.from_dict(options)
        self._ctx: PluginContext | None = None
        self._patterns: list[re.Pattern[str]] = []
        self._compile_patterns()
        # Cache: path -> _CachedRead (keyed by (path, mtime))
        self._cache: dict[Path, _CachedRead] = {}
        # One-shot payload when reload_per_turn is False.
        self._cached_payload: str | None = None
        self._injection_audit: list[dict[str, Any]] = []

    # ── Lifecycle ────────────────────────────────────────────────────

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context
        logger.info(
            "context_files loaded",
            files=len(self._opts.files),
            patterns=len(self._patterns),
            action=self._opts.injection_action,
        )

    async def on_unload(self) -> None:
        self._cache.clear()
        self._cached_payload = None

    # ── Hook ─────────────────────────────────────────────────────────

    def should_apply(self, context: PluginContext) -> bool:
        """Optional agent-name restriction. Empty list = all agents."""
        if not self._opts.agent_names:
            return True
        return context.agent_name in self._opts.agent_names

    async def pre_llm_call(
        self, messages: list[dict], **kwargs: Any
    ) -> list[dict] | None:
        if not self._opts.enabled:
            return None
        try:
            if self._ctx is not None and not self.should_apply(self._ctx):
                return None
            if self._already_injected(messages):
                return None
            payload = self._build_payload()
            if not payload:
                return None
            return self._inject(messages, payload)
        except Exception as exc:  # never raise from the hook
            logger.warning("context_files hook failed", error=str(exc))
            return None

    # ── Payload assembly ─────────────────────────────────────────────

    def _build_payload(self) -> str | None:
        """Return the combined injection payload, or None if nothing to inject."""
        if not self._opts.reload_per_turn and self._cached_payload is not None:
            return self._cached_payload or None

        root = self._resolve_walk_root()
        stop = self._resolve_stop_anchor(root)
        matches = self._discover_files(root, stop)
        if not matches:
            self._cached_payload = ""
            return None

        blocks: list[str] = []
        total = 0
        for rel_name, path in matches:
            entry = self._read_with_cache(path)
            if entry is None:
                continue
            body = entry.content
            # Enforce per-file cap (cache already clipped, but keep guard).
            if (
                len(body.encode("utf-8", errors="ignore"))
                > self._opts.max_per_file_bytes
            ):
                body = body[: self._opts.max_per_file_bytes]
            # Enforce total cap.
            remaining = self._opts.max_total_bytes - total
            if remaining <= 0:
                break
            body_bytes = body.encode("utf-8", errors="ignore")
            if len(body_bytes) > remaining:
                body = body_bytes[:remaining].decode("utf-8", errors="ignore")
            header = f"### {rel_name}  ({path})"
            if entry.redacted and self._opts.injection_action == "block":
                block_text = (
                    f"{header}\n"
                    f"[REDACTED: prompt-injection patterns detected — "
                    f"{', '.join(entry.patterns_hit)}]"
                )
            elif entry.redacted:  # annotate
                block_text = (
                    f"{header}\n"
                    f"[WARNING: suspicious patterns in this file — "
                    f"{', '.join(entry.patterns_hit)}]\n\n{body}"
                )
            else:
                block_text = f"{header}\n{body}"
            blocks.append(block_text)
            total += len(block_text.encode("utf-8", errors="ignore"))
            if total >= self._opts.max_total_bytes:
                break

        if not blocks:
            self._cached_payload = ""
            return None

        payload = "\n\n".join([SENTINEL, self._opts.preamble, ""] + blocks + [SENTINEL])
        self._cached_payload = payload
        return payload

    # ── Injection ────────────────────────────────────────────────────

    def _inject(self, messages: list[dict], payload: str) -> list[dict]:
        """Insert the payload per the configured position."""
        new = list(messages)
        if self._opts.position == "prepend_last_user":
            for i in range(len(new) - 1, -1, -1):
                if new[i].get("role") == "user":
                    original = new[i].get("content", "")
                    if isinstance(original, list):
                        # Multimodal content — prepend a text part.
                        new[i] = {
                            **new[i],
                            "content": [{"type": "text", "text": payload + "\n\n"}]
                            + original,
                        }
                    else:
                        new[i] = {
                            **new[i],
                            "content": f"{payload}\n\n{original}",
                        }
                    return new
            # No user message found — fall through to after_system.

        # Default: after_system (insert as a user-role message at index 1
        # if system is present; otherwise at index 0).
        insert_idx = 0
        for i, msg in enumerate(new):
            if msg.get("role") == "system":
                insert_idx = i + 1
            else:
                break
        new.insert(insert_idx, {"role": "user", "content": payload})
        return new

    def _already_injected(self, messages: list[dict]) -> bool:
        """Detect our sentinel in any existing message to avoid double-injection."""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str) and SENTINEL in content:
                return True
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text") or part.get("content") or ""
                        if isinstance(text, str) and SENTINEL in text:
                            return True
        return False

    # ── Discovery ────────────────────────────────────────────────────

    def _resolve_walk_root(self) -> Path:
        mode = self._opts.walk_from
        if mode == "cwd":
            return Path.cwd()
        if mode == "agent_path":
            if self._ctx and self._ctx.working_dir:
                return Path(self._ctx.working_dir)
            return Path.cwd()
        # Treat as fixed path.
        try:
            p = Path(mode).expanduser().resolve()
            if p.exists():
                return p
        except Exception:
            pass
        return Path.cwd()

    def _resolve_stop_anchor(self, start: Path) -> Path | None:
        mode = self._opts.stop_at
        if mode == "git_root":
            return _find_git_root(start)
        if mode == "filesystem_root":
            return None
        try:
            p = Path(mode).expanduser().resolve()
            if p.exists():
                return p
        except Exception:
            pass
        return _find_git_root(start)

    def _discover_files(self, start: Path, stop: Path | None) -> list[tuple[str, Path]]:
        """Walk from ``start`` upwards looking for configured filenames.

        Returns a list of ``(relative_display_name, absolute_path)``
        pairs. Order follows the user's ``files`` list (priority); the
        first occurrence of each name wins.
        """
        seen_names: set[str] = set()
        hits: list[tuple[str, Path]] = []

        current = start.resolve() if start.exists() else start
        stop_abs = stop.resolve() if stop and stop.exists() else None

        # Walk up, bounded to avoid infinite loops on weird FS layouts.
        for _ in range(64):
            for name in self._opts.files:
                if name in seen_names:
                    continue
                candidate = current / name
                try:
                    if candidate.is_file():
                        hits.append((name, candidate))
                        seen_names.add(name)
                except OSError:
                    continue
            if stop_abs is not None and current == stop_abs:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent

        # Reorder to match configured priority (first wins on duplicates
        # which can't actually happen here because of `seen_names`, but
        # keep the output stable).
        priority = {name: idx for idx, name in enumerate(self._opts.files)}
        hits.sort(key=lambda kv: priority.get(kv[0], 1_000))
        return hits

    # ── File reading + cache + injection scan ────────────────────────

    def _read_with_cache(self, path: Path) -> _CachedRead | None:
        try:
            stat = path.stat()
        except OSError as exc:
            logger.debug("context_files stat failed", path=str(path), error=str(exc))
            return None

        cached = self._cache.get(path)
        if (
            cached is not None
            and cached.mtime == stat.st_mtime
            and cached.size == stat.st_size
        ):
            return cached

        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.debug("context_files read failed", path=str(path), error=str(exc))
            return None

        cap = self._opts.max_per_file_bytes
        truncated = raw[:cap] if cap > 0 and len(raw) > cap else raw
        try:
            text = truncated.decode("utf-8", errors="replace")
        except Exception:
            text = truncated.decode("latin-1", errors="replace")

        patterns_hit = self._scan(text)
        redacted = bool(patterns_hit)
        if redacted and self._opts.injection_action == "block":
            content = ""  # body suppressed downstream; header still emitted
        else:
            content = text

        entry = _CachedRead(
            mtime=stat.st_mtime,
            size=stat.st_size,
            content=content,
            redacted=redacted,
            patterns_hit=patterns_hit,
        )
        self._cache[path] = entry
        if redacted:
            self._injection_audit.append(
                {
                    "path": str(path),
                    "patterns": patterns_hit,
                    "action": self._opts.injection_action,
                }
            )
            self._record_audit(path, patterns_hit)
            logger.warning(
                "context_files injection hit",
                path=str(path),
                patterns=",".join(patterns_hit),
                action=self._opts.injection_action,
            )
        return entry

    # ── Injection scan ───────────────────────────────────────────────

    def _compile_patterns(self) -> None:
        source = self._opts.injection_patterns
        if source is None:
            source = DEFAULT_INJECTION_PATTERNS
        for raw in source:
            try:
                self._patterns.append(re.compile(raw))
            except re.error as exc:
                logger.warning(
                    "context_files invalid pattern — skipped",
                    pattern=raw,
                    error=str(exc),
                )

    def _scan(self, text: str) -> list[str]:
        """Return the list of pattern sources that matched ``text``."""
        hits: list[str] = []
        for pat in self._patterns:
            if pat.search(text):
                hits.append(pat.pattern)
        return hits

    # ── Audit ────────────────────────────────────────────────────────

    def _record_audit(self, path: Path, patterns: list[str]) -> None:
        """Append an audit entry into the agent's scratchpad if available."""
        if self._ctx is None:
            return
        scratchpad = self._ctx.scratchpad
        if scratchpad is None:
            return
        # Best-effort: try a generic append API, else fall back to set_state.
        try:
            if hasattr(scratchpad, "append"):
                scratchpad.append(
                    "context_files_audit",
                    {"path": str(path), "patterns": patterns},
                )
                return
        except Exception as exc:
            logger.debug("scratchpad append failed", error=str(exc))
        try:
            key = "context_files_audit"
            existing = self._ctx.get_state(key) or []
            existing.append({"path": str(path), "patterns": patterns})
            self._ctx.set_state(key, existing)
        except Exception as exc:
            logger.debug("audit set_state failed", error=str(exc))


# ── Helpers ──────────────────────────────────────────────────────────


def _find_git_root(start: Path) -> Path | None:
    """Walk up from *start* looking for a ``.git`` directory or file.

    Returns the directory containing ``.git`` or ``None`` if none found.
    Worktrees use a ``.git`` file rather than a directory, so we accept
    either.
    """
    try:
        current = start.resolve()
    except OSError:
        current = start
    for _ in range(64):
        git_marker = current / ".git"
        try:
            if git_marker.exists():
                return current
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None
