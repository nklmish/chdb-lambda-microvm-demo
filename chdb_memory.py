"""chDB-native agent memory — append-only MergeTree with time-travel.

This is the blog's Pillar-1 ("chDB is what your agent thinks with") made real, and
it is deliberately *not* AgentCore Memory: the memory lives in the same local chDB
store the agent already queries, so on AWS Lambda MicroVMs it persists across
suspend/resume for free (the same persistent disk that carries the federation
cache in Demo 2).

The model is the blog's exactly: **memory is a history, so don't update it —
append to it.** Every revision is a new immutable row with a monotonically
increasing ``version``; deletes are a soft ``is_deleted`` flag. That one table
answers three questions:

  * **current state / recall** — the latest non-deleted version per key
  * **full history** — every revision of a belief, in order
  * **point-in-time ("time travel")** — what the agent believed as of version *t*

All access goes through one long-lived ``chdb.session.Session`` under a re-entrant
lock — the same Pillar-1 pattern federation_tools uses (the process-global chDB
embedded server allows one connection per process; a shared Session avoids the
per-call reload race).

Vector recall is a drop-in: add an ``embedding Array(Float32)`` column and rank by
``cosineDistance`` inside the same query (see the blog). This module keeps the
structured time-travel core so the "wow" — querying the *past* of a belief — needs
no embedding model.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from chdb import session as _chs

_TABLE = "agent_state.beliefs"

_session_lock = threading.RLock()
_session: "object | None" = None
_session_path: "str | None" = None


def _get_session(path: str):
    """Return a process-wide Session for ``path`` (recreated if the path changes)."""
    global _session, _session_path
    if _session is None or _session_path != path:
        if _session is not None:
            try:
                _session.close()
            except Exception:  # noqa: BLE001
                pass
        _session = _chs.Session(path)
        _session_path = path
    return _session


def close_session() -> None:
    """Close the process-global memory Session and release the embedded connection.

    chDB's embedded server allows one connection per process, so a long-lived
    Session must be released before another chDB user (a different store path) opens
    one — e.g. between tests, or before handing the process back to the agent's own
    chDB access. Safe to call repeatedly.
    """
    global _session, _session_path
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:  # noqa: BLE001
                pass
        _session = None
        _session_path = None


def _rows(path: str, sql: str) -> list[dict]:
    with _session_lock:
        res = _get_session(path).query(sql, "JSON")
        raw = res.bytes() if (res is not None and hasattr(res, "bytes")) else b""
    return json.loads(raw).get("data", []) if raw else []


def _exec(path: str, sql: str) -> None:
    with _session_lock:
        _get_session(path).query(sql)


def _q(s: str) -> str:
    """Single-quote-escape a string for inline SQL."""
    return s.replace("'", "''")


@dataclass(frozen=True)
class Belief:
    key: str
    content: str
    version: int
    is_deleted: bool


class MemoryStore:
    """Append-only, time-travelling agent memory backed by a chDB MergeTree.

    Args:
        path: chDB data directory (the agent's store). Defaults to the same
            ``CHDB_DATA_PATH`` the rest of the app uses.
    """

    def __init__(self, path: str | None = None) -> None:
        import os

        self.path = path or os.getenv("CHDB_DATA_PATH", "/app/local_chdb_data")
        self._ensure_table()

    def _ensure_table(self) -> None:
        _exec(self.path, "CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic")
        _exec(
            self.path,
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "key String, content String, version UInt64, is_deleted UInt8 DEFAULT 0, "
            "created_at DateTime64(3) DEFAULT now64(3)"
            ") ENGINE = MergeTree ORDER BY (key, version)",
        )

    def _next_version(self) -> int:
        """Next GLOBAL version. Versions are a single monotonic timeline across all
        keys (not per-key), so ``as_of(t)`` is a coherent snapshot of every belief
        as it stood at that point — the blog's point-in-time query."""
        rows = _rows(self.path, f"SELECT max(version) AS v FROM {_TABLE}")
        cur = rows[0].get("v") if rows else None
        return (int(cur) + 1) if cur not in (None, "") else 1

    # -- write path (append-only) --------------------------------------------

    def remember(self, key: str, content: str) -> int:
        """Append a new belief (or a revision of an existing one). Returns the version."""
        version = self._next_version()
        _exec(
            self.path,
            f"INSERT INTO {_TABLE} (key, content, version, is_deleted) "
            f"VALUES ('{_q(key)}', '{_q(content)}', {version}, 0)",
        )
        return version

    # `revise` is intentionally an alias: a revision is just the next append.
    revise = remember

    def forget(self, key: str) -> int:
        """Soft-delete a belief (append an is_deleted=1 row). Returns the version."""
        version = self._next_version()
        _exec(
            self.path,
            f"INSERT INTO {_TABLE} (key, content, version, is_deleted) "
            f"VALUES ('{_q(key)}', '', {version}, 1)",
        )
        return version

    # -- read paths ----------------------------------------------------------

    def recall(self, limit: int = 50) -> list[Belief]:
        """Current state: latest version per key, deletes dropped.

        Order matters (per the blog): take each key's latest version FIRST, then
        drop deletes — filtering is_deleted first would resurrect the prior
        version of a deleted belief.
        """
        rows = _rows(
            self.path,
            "SELECT key, content, version, is_deleted FROM ("
            f"SELECT * FROM {_TABLE} ORDER BY key, version DESC LIMIT 1 BY key"
            ") WHERE is_deleted = 0 ORDER BY key "
            f"LIMIT {int(limit)}",
        )
        return [_belief(r) for r in rows]

    def history(self, key: str) -> list[Belief]:
        """Full revision history of one belief, oldest to newest."""
        rows = _rows(
            self.path,
            f"SELECT key, content, version, is_deleted FROM {_TABLE} "
            f"WHERE key = '{_q(key)}' ORDER BY version",
        )
        return [_belief(r) for r in rows]

    def as_of(self, version: int, limit: int = 50) -> list[Belief]:
        """Time travel: what the agent believed as of ``version`` (inclusive)."""
        rows = _rows(
            self.path,
            "SELECT key, content, version, is_deleted FROM ("
            f"SELECT * FROM {_TABLE} WHERE version <= {int(version)} "
            "ORDER BY key, version DESC LIMIT 1 BY key"
            ") WHERE is_deleted = 0 ORDER BY key "
            f"LIMIT {int(limit)}",
        )
        return [_belief(r) for r in rows]


def _belief(r: dict) -> Belief:
    return Belief(
        key=str(r["key"]),
        content=str(r["content"]),
        version=int(r["version"]),
        is_deleted=bool(int(r["is_deleted"])),
    )
