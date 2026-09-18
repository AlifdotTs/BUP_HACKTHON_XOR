"""Small SQLite-backed request metrics store for local testing and demos."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
from typing import Any


_DEFAULT_DB_PATH = "data/request_metrics.sqlite3"


def _database_path() -> Path:
    path = Path(os.environ.get("METRICS_DB_PATH", _DEFAULT_DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def initialize_metrics_db() -> None:
    with sqlite3.connect(_database_path(), timeout=5) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS request_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                latency_ms REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_request_metrics_created_at
            ON request_metrics(created_at DESC)
            """
        )


def record_request(method: str, path: str, status_code: int, latency_ms: float) -> None:
    """Persist non-sensitive request metadata; never persist headers or bodies."""

    try:
        initialize_metrics_db()
        with sqlite3.connect(_database_path(), timeout=5) as connection:
            connection.execute(
                """
                INSERT INTO request_metrics
                    (created_at, method, path, status_code, latency_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    method,
                    path,
                    int(status_code),
                    round(float(latency_ms), 3),
                ),
            )
    except sqlite3.Error:
        # Metrics must never make a valid optimization request fail.
        return


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 3)


def get_metrics(recent_limit: int = 20) -> dict[str, Any]:
    """Return aggregate metrics and recent request rows for the dashboard."""

    initialize_metrics_db()
    with sqlite3.connect(_database_path(), timeout=5) as connection:
        rows = connection.execute(
            """
            SELECT created_at, method, path, status_code, latency_ms
            FROM request_metrics
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(recent_limit, 100)),),
        ).fetchall()
        all_latencies = [
            float(row[0])
            for row in connection.execute(
                "SELECT latency_ms FROM request_metrics"
            ).fetchall()
        ]
        total = connection.execute(
            "SELECT COUNT(*) FROM request_metrics"
        ).fetchone()[0]
        successes = connection.execute(
            "SELECT COUNT(*) FROM request_metrics WHERE status_code BETWEEN 200 AND 399"
        ).fetchone()[0]

    return {
        "request_count": int(total),
        "success_count": int(successes),
        "error_count": int(total - successes),
        "p95_latency_ms": _percentile(all_latencies, 0.95),
        "recent_requests": [
            {
                "created_at": row[0],
                "method": row[1],
                "path": row[2],
                "status_code": row[3],
                "latency_ms": row[4],
            }
            for row in rows
        ],
    }
