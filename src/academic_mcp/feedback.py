"""Durable, MCP-native feedback inbox for users and calling agents."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_VALID_CATEGORIES = {"bug", "reliability", "feature", "usability", "documentation", "other"}
_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_STATUSES = {"open", "triaged", "in_progress", "resolved", "wont_fix"}


def _db_path() -> Path:
    configured = os.getenv("FEEDBACK_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    cache = Path(os.getenv("PDF_CACHE_DIR", "/var/cache/academic-mcp/pdfs")).expanduser()
    return cache.parent / "feedback.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            summary TEXT NOT NULL,
            details TEXT NOT NULL,
            tool_name TEXT,
            reproduction_steps TEXT NOT NULL,
            expected_behavior TEXT,
            actual_behavior TEXT,
            client TEXT,
            context TEXT,
            resolution TEXT
        )"""
    )
    return conn


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def submit(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _clean(payload.get("summary"), 300)
    details = _clean(payload.get("details"), 20_000)
    if not summary or not details:
        raise ValueError("summary and details are required")
    category = _clean(payload.get("category") or "other", 32).lower()
    severity = _clean(payload.get("severity") or "medium", 16).lower()
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(sorted(_VALID_CATEGORIES))}")
    if severity not in _VALID_SEVERITIES:
        raise ValueError(f"severity must be one of: {', '.join(sorted(_VALID_SEVERITIES))}")
    steps = payload.get("reproduction_steps") or []
    if not isinstance(steps, list):
        raise ValueError("reproduction_steps must be an array of strings")
    steps = [_clean(step, 2_000) for step in steps[:25] if _clean(step, 2_000)]
    now = datetime.now(UTC).isoformat()
    feedback_id = f"fb_{uuid.uuid4().hex[:12]}"
    values = (
        feedback_id, now, now, "open", category, severity, summary, details,
        _clean(payload.get("tool_name"), 100) or None, json.dumps(steps),
        _clean(payload.get("expected_behavior"), 5_000) or None,
        _clean(payload.get("actual_behavior"), 5_000) or None,
        _clean(payload.get("client"), 200) or None,
        _clean(payload.get("context"), 10_000) or None, None,
    )
    with _connect() as conn:
        conn.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    return {"id": feedback_id, "status": "open", "created_at": now}


def list_items(status: str | None = "open", limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    params: list[Any] = []
    where = ""
    if status:
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(_VALID_STATUSES))}")
        where = " WHERE status = ?"
        params.append(status)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,created_at,updated_at,status,category,severity,summary,tool_name "
            f"FROM feedback{where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [dict(row) for row in rows]


def get(feedback_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["reproduction_steps"] = json.loads(result["reproduction_steps"] or "[]")
    return result


def update(feedback_id: str, status: str, resolution: str = "") -> dict[str, Any]:
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(_VALID_STATUSES))}")
    now = datetime.now(UTC).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE feedback SET status=?, resolution=?, updated_at=? WHERE id=?",
            (status, _clean(resolution, 10_000) or None, now, feedback_id),
        )
    if cursor.rowcount != 1:
        raise ValueError(f"feedback item not found: {feedback_id}")
    return {"id": feedback_id, "status": status, "updated_at": now}
