"""SQLite metadata and snapshot store for reproducible local experiments.

SQLite is the initial local experiment adapter. The repository boundary keeps
storage replaceable with MySQL or an object store in a later implementation.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any


class SQLiteCheckpointStore:
    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    parent_checkpoint_id TEXT,
                    snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    is_safe INTEGER NOT NULL CHECK (is_safe IN (0, 1)),
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, sequence)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_task_safe_sequence "
                "ON checkpoints(task_id, is_safe, sequence DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    checkpoint_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def insert_checkpoint(self, record: dict[str, Any]) -> int:
        # Reserve the next task-local sequence and insert the snapshot in one
        # write transaction so concurrent writers cannot claim the same number.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM checkpoints WHERE task_id = ?",
                (record["task_id"],),
            ).fetchone()
            record["sequence"] = int(row["next_sequence"])
            connection.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, task_id, sequence, parent_checkpoint_id,
                    snapshot_json, snapshot_sha256, is_safe, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["checkpoint_id"], record["task_id"], record["sequence"],
                    record["parent_checkpoint_id"], record["snapshot_json"],
                    record["snapshot_sha256"], int(record["is_safe"]),
                    record["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    task_id, event_type, checkpoint_id, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record["task_id"], "CHECKPOINT_CREATED", record["checkpoint_id"],
                    json.dumps({"safe": record["is_safe"], "sequence": record["sequence"]}, sort_keys=True),
                    record["created_at"],
                ),
            )
            return record["sequence"]

    def latest_safe_checkpoint(self, task_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """SELECT * FROM checkpoints
                   WHERE task_id = ? AND is_safe = 1
                   ORDER BY sequence DESC LIMIT 1""",
                (task_id,),
            ).fetchone()

    def add_audit_event(
        self, task_id: str, event_type: str, checkpoint_id: str,
        details: dict[str, Any], created_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO audit_events
                   (task_id, event_type, checkpoint_id, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (task_id, event_type, checkpoint_id, json.dumps(details, sort_keys=True), created_at),
            )

    def get_audit_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE task_id = ? ORDER BY event_id",
                (task_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "checkpoint_id": row["checkpoint_id"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def tamper_snapshot_for_test(self, checkpoint_id: str, snapshot_json: str) -> None:
        """Test helper: deliberately corrupt a stored snapshot without its digest."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE checkpoints SET snapshot_json = ? WHERE checkpoint_id = ?",
                (snapshot_json, checkpoint_id),
            )
