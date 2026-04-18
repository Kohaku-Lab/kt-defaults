"""Database Tool — execute SQL queries against SQLite databases.

Supports read-only queries by default. Write queries can be enabled
via the ``allow_write`` option.

Usage in config.yaml:

    tools:
      - name: database
        type: custom
        module: kt_biome.tools.database
        class_name: DatabaseTool
        options:
          path: ./data/my.db      # path to SQLite database
          allow_write: false       # set true to allow INSERT/UPDATE/DELETE
          max_rows: 100            # max rows returned per query
"""

import sqlite3
from pathlib import Path
from typing import Any

from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

# Statements that modify data
_WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE"}


class DatabaseTool(BaseTool):
    """Execute SQL queries against a SQLite database."""

    needs_context = True

    def __init__(self, config=None, options: dict[str, Any] | None = None):
        super().__init__(config)
        opts = options or {}
        self._db_path = opts.get("path", "")
        self._allow_write = bool(opts.get("allow_write", False))
        self._max_rows = int(opts.get("max_rows", 100))
        self._conn: sqlite3.Connection | None = None

    @property
    def tool_name(self) -> str:
        return "database"

    @property
    def description(self) -> str:
        mode = "read/write" if self._allow_write else "read-only"
        return f"Execute SQL queries against a SQLite database ({mode})"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL query to execute",
                },
                "params": {
                    "type": "array",
                    "description": "Query parameters for parameterized queries",
                    "items": {"type": "string"},
                },
            },
            "required": ["query"],
        }

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self._db_path:
                raise ValueError("No database path configured")
            db_path = Path(self._db_path).expanduser().resolve()
            if not db_path.exists():
                raise FileNotFoundError(f"Database not found: {db_path}")
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _is_write_query(self, query: str) -> bool:
        first_word = query.strip().split()[0].upper() if query.strip() else ""
        return first_word in _WRITE_KEYWORDS

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        query = args.get("query", "").strip()
        params = args.get("params", [])

        if not query:
            return ToolResult(error="No query provided")

        # Check write permission
        if self._is_write_query(query) and not self._allow_write:
            return ToolResult(
                error="Write queries are not allowed. "
                "Set allow_write: true in tool options to enable."
            )

        try:
            conn = self._get_conn()
            cursor = conn.execute(query, params or [])

            if self._is_write_query(query):
                conn.commit()
                return ToolResult(
                    output=f"Query executed. Rows affected: {cursor.rowcount}",
                    exit_code=0,
                )

            # Read query — format results
            rows = cursor.fetchmany(self._max_rows + 1)
            if not rows:
                return ToolResult(output="(no results)", exit_code=0)

            # Get column names
            columns = [desc[0] for desc in cursor.description]
            truncated = len(rows) > self._max_rows
            if truncated:
                rows = rows[: self._max_rows]

            # Format as table
            lines = [" | ".join(columns)]
            lines.append("-+-".join("-" * max(len(c), 3) for c in columns))
            for row in rows:
                lines.append(" | ".join(str(v) for v in row))

            output = "\n".join(lines)
            if truncated:
                output += f"\n\n... (showing first {self._max_rows} rows)"

            logger.debug(
                "Database query",
                query=query[:100],
                rows=len(rows),
            )

            return ToolResult(output=output, exit_code=0)

        except Exception as e:
            logger.error("Database query failed", error=str(e))
            return ToolResult(error=f"SQL error: {e}")

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return (
            "# database\n\n"
            "Execute SQL queries against a SQLite database.\n\n"
            "## Parameters\n"
            "- `query` (required): SQL query string\n"
            "- `params` (optional): list of parameters for parameterized queries\n\n"
            "## Examples\n"
            "- `SELECT * FROM users WHERE age > 25`\n"
            "- `SELECT name, COUNT(*) FROM orders GROUP BY name`\n"
            "- `PRAGMA table_info(users)` — show table schema\n"
            "- `.tables` equivalent: `SELECT name FROM sqlite_master WHERE type='table'`\n"
        )
