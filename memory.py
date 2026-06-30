"""In-process session memory. Writes sanitized conversation and analysis rows
to chDB via db.execute() and reads them back via db.query_records().

Contract:
    class AnalyticalMemory:
        def save_conversation(self, role: str, content: str) -> None
        def get_recent_context(self, limit: int = 5) -> str
        def save_analysis(self, description: str, parameters: dict,
                          result_summary: str, execution_ms: int) -> None
        def get_recent_analyses(self, limit: int = 3) -> str

Truncation is applied BEFORE _sanitize so we never cut mid-escape and
produce invalid SQL.
"""
from __future__ import annotations
import json

import db

_MAX_CONTENT = 5000
_MAX_RESULT_SUMMARY = 1000


def _sanitize(s: str) -> str:
    """SQL-literal sanitize. Order matters:
      1. strip null bytes (ClickHouse rejects)
      2. double backslashes (so step 3 can't mis-escape a backslash pair)
      3. double single quotes (SQL-standard literal escape)
      4. strip other control chars, preserving \\t \\n \\r
    """
    s = s.replace("\x00", "")
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "''")
    return "".join(c for c in s if ord(c) >= 0x20 or c in "\t\n\r")


class AnalyticalMemory:
    """Fast, in-process session memory backed by chDB."""

    def save_conversation(self, role: str, content: str) -> None:
        # Truncate FIRST, sanitize SECOND — never cut mid-escape.
        content = content[:_MAX_CONTENT]
        role_s = _sanitize(role)
        content_s = _sanitize(content)
        db.execute(
            f"INSERT INTO agent_state.conversations (role, content) "
            f"VALUES ('{role_s}', '{content_s}')"
        )

    def get_recent_context(self, limit: int = 5) -> str:
        rows = db.query_records(
            f"SELECT role, content FROM agent_state.conversations "
            f"ORDER BY created_at DESC LIMIT {int(limit)}"
        )
        # Reverse so output is oldest→newest for the model.
        return "\n".join(f"[{r['role']}]: {r['content']}" for r in reversed(rows))

    def save_analysis(
        self,
        description: str,
        parameters: dict,
        result_summary: str,
        execution_ms: int,
    ) -> None:
        # Truncate FIRST, sanitize SECOND.
        result_summary = result_summary[:_MAX_RESULT_SUMMARY]
        desc_s = _sanitize(description)
        params_s = _sanitize(json.dumps(parameters))
        summary_s = _sanitize(result_summary)
        db.execute(
            f"INSERT INTO agent_state.analysis_log "
            f"(description, parameters, result_summary, execution_ms) "
            f"VALUES ('{desc_s}', '{params_s}', '{summary_s}', {int(execution_ms)})"
        )

    def get_recent_analyses(self, limit: int = 3) -> str:
        rows = db.query_records(
            f"SELECT description, execution_ms FROM agent_state.analysis_log "
            f"ORDER BY created_at DESC LIMIT {int(limit)}"
        )
        return "\n".join(
            f"- {r['description']} ({r['execution_ms']}ms)" for r in rows
        )
