"""SQLite and content-addressed storage for Core execution traces."""

from __future__ import annotations

import asyncio
import json
import os
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .models import INLINE_ARTIFACT_BYTES, TERMINAL_TRACE_STATUSES

_CAPACITY_CLEANUP_BATCH_SIZE = 32
_CAPACITY_CLEANUP_MIN_TRACE_ESTIMATE_BYTES = 64 * 1024


class TraceStorageError(RuntimeError):
    """Raised when the observability store cannot complete a requested action."""


class TraceNotFoundError(TraceStorageError):
    """Raised when a trace or artifact is not present in the store."""


class TraceDeleteConflictError(TraceStorageError):
    """Raised when a caller attempts to delete a running trace."""


@dataclass
class StoreCommand:
    """A write operation consumed by the single Core trace writer.

    Args:
        action: Internal storage operation name.
        payload: JSON-compatible operation data, with bytes allowed for artifact bodies.
    """

    action: str
    payload: dict[str, Any]


class TraceStore:
    """Own the durable trace index and content-addressed artifact files.

    Mutations use one owned SQLite connection. Queries use a dedicated read-only
    connection so WebUI reads cannot interleave their cursors with writer
    transactions on the same aiosqlite connection.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.database_path = root / "trace.db"
        self.objects_path = root / "objects"
        self.tmp_path = root / "tmp"
        self._db: aiosqlite.Connection | None = None
        self._read_db: aiosqlite.Connection | None = None
        self._mutation_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the trace directory, schema, and SQLite write settings."""

        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_path.mkdir(parents=True, exist_ok=True)
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.database_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        auto_vacuum_row = await _fetch_one(self._db, "PRAGMA auto_vacuum")
        if auto_vacuum_row is not None and int(auto_vacuum_row[0]) != 2:
            await self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            await self._db.execute("VACUUM")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                root_span_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                plugin_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                status TEXT NOT NULL,
                outcome TEXT,
                degraded INTEGER NOT NULL DEFAULT 0,
                degradation_reasons_json TEXT NOT NULL DEFAULT '[]',
                attributes_json TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 0,
                dropped_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS traces_list_idx
            ON traces (ended_at DESC, trace_id DESC);
            CREATE INDEX IF NOT EXISTS traces_list_order_idx
            ON traces (COALESCE(ended_at, started_at) DESC, trace_id DESC);
            CREATE INDEX IF NOT EXISTS traces_status_idx
            ON traces (status, ended_at DESC, trace_id DESC);
            CREATE INDEX IF NOT EXISTS traces_operation_idx
            ON traces (operation, ended_at DESC, trace_id DESC);
            CREATE INDEX IF NOT EXISTS traces_source_idx
            ON traces (source, ended_at DESC, trace_id DESC);
            CREATE INDEX IF NOT EXISTS traces_kind_idx
            ON traces (kind, ended_at DESC, trace_id DESC);
            CREATE INDEX IF NOT EXISTS traces_plugin_idx
            ON traces (plugin_id, ended_at DESC, trace_id DESC);

            CREATE TABLE IF NOT EXISTS spans (
                trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                operation TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                plugin_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                status TEXT NOT NULL,
                outcome TEXT,
                degraded INTEGER NOT NULL DEFAULT 0,
                degradation_reasons_json TEXT NOT NULL DEFAULT '[]',
                attributes_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (trace_id, span_id)
            );

            CREATE INDEX IF NOT EXISTS spans_trace_parent_idx
            ON spans (trace_id, parent_span_id, started_at, span_id);
            CREATE INDEX IF NOT EXISTS spans_trace_status_started_idx
            ON spans (trace_id, status, started_at DESC, span_id DESC);

            CREATE TABLE IF NOT EXISTS events (
                trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                span_id TEXT NOT NULL,
                event_index INTEGER NOT NULL,
                name TEXT NOT NULL,
                occurred_at REAL NOT NULL,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (trace_id, span_id, event_index)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                content_hash TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                logical_size INTEGER NOT NULL,
                captured_size INTEGER NOT NULL,
                stored_size INTEGER NOT NULL,
                codec TEXT NOT NULL,
                inline_body BLOB,
                storage_key TEXT,
                truncated INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifact_refs (
                trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                span_id TEXT NOT NULL,
                ref_index INTEGER NOT NULL,
                content_hash TEXT NOT NULL REFERENCES artifacts(content_hash),
                role TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (trace_id, span_id, ref_index)
            );

            CREATE INDEX IF NOT EXISTS artifact_refs_hash_idx
            ON artifact_refs (content_hash);

            CREATE TABLE IF NOT EXISTS links (
                trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                span_id TEXT NOT NULL,
                link_index INTEGER NOT NULL,
                relation TEXT NOT NULL,
                target_trace_id TEXT,
                target_span_id TEXT,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (trace_id, span_id, link_index)
            );

            """
        )
        await self._db.commit()
        self._read_db = await aiosqlite.connect(self.database_path)
        self._read_db.row_factory = aiosqlite.Row
        query_only_cursor = await self._read_db.execute("PRAGMA query_only=ON")
        await query_only_cursor.close()

    async def close(self) -> None:
        """Close the SQLite connection owned by this store."""

        if self._read_db is not None:
            await self._read_db.close()
            self._read_db = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def mark_running_incomplete(self) -> int:
        """Mark traces left running by a prior process as incomplete.

        Returns:
            Number of root traces that were repaired.
        """

        db = self._require_db()
        now = time.time()
        cursor = await db.execute(
            """
            UPDATE traces
            SET status = 'incomplete', outcome = 'process_restart', ended_at = ?,
                revision = revision + 1
            WHERE status = 'running'
            """,
            (now,),
        )
        await db.execute(
            """
            UPDATE spans
            SET status = 'incomplete', outcome = 'process_restart', ended_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )
        await db.commit()
        return cursor.rowcount

    async def apply_batch(self, commands: list[StoreCommand]) -> None:
        """Serialize durable writer batches with maintenance mutations."""

        if not commands:
            return
        async with self._mutation_lock:
            await self._apply_batch(commands)

    async def _apply_batch(self, commands: list[StoreCommand]) -> None:
        """Persist a writer batch with one SQLite transaction.

        Artifact files are written before the transaction.  A crash can leave an
        unreferenced object for background garbage collection, but it cannot
        normally create a database reference to a file that was never renamed.

        Args:
            commands: Commands accepted by the asynchronous trace writer.
        """

        if not commands:
            return
        db = self._require_db()
        artifact_payloads: dict[str, list[dict[str, Any]]] = {}
        for command in commands:
            if command.action != "artifact_ref":
                continue
            payload = command.payload
            content_hash = payload["content_hash"]
            artifact_payloads.setdefault(content_hash, []).append(payload)

        for payloads in artifact_payloads.values():
            primary = payloads[0]
            await self._prepare_artifact(primary)
            for duplicate in payloads[1:]:
                duplicate.update(
                    {
                        key: primary.get(key)
                        for key in (
                            "codec",
                            "stored_size",
                            "inline_body",
                            "storage_key",
                        )
                    }
                )

        try:
            await db.execute("BEGIN")
            for command in commands:
                await self._apply_command(command)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _apply_command(self, command: StoreCommand) -> None:
        db = self._require_db()
        payload = command.payload
        if command.action == "trace_create":
            await db.execute(
                """
                INSERT INTO traces (
                    trace_id, root_span_id, operation, kind, source, plugin_id,
                    started_at, status, outcome, degraded,
                    degradation_reasons_json, attributes_json, revision, dropped_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["trace_id"],
                    payload["root_span_id"],
                    payload["operation"],
                    payload["kind"],
                    payload["source"],
                    payload.get("plugin_id"),
                    payload["started_at"],
                    payload["status"],
                    payload.get("outcome"),
                    int(payload.get("degraded", False)),
                    _encode_json(payload.get("degradation_reasons", [])),
                    _encode_json(payload.get("attributes", {})),
                    payload.get("revision", 0),
                    _encode_json(payload.get("dropped", {})),
                ),
            )
            return
        if command.action == "trace_patch":
            await db.execute(
                """
                UPDATE traces
                SET ended_at = ?, status = ?, outcome = ?, degraded = ?,
                    degradation_reasons_json = ?, attributes_json = ?,
                    revision = ?, dropped_json = ?
                WHERE trace_id = ? AND status = 'running'
                """,
                (
                    payload.get("ended_at"),
                    payload["status"],
                    payload.get("outcome"),
                    int(payload.get("degraded", False)),
                    _encode_json(payload.get("degradation_reasons", [])),
                    _encode_json(payload.get("attributes", {})),
                    payload["revision"],
                    _encode_json(payload.get("dropped", {})),
                    payload["trace_id"],
                ),
            )
            return
        if command.action == "span_create":
            await db.execute(
                """
                INSERT INTO spans (
                    trace_id, span_id, parent_span_id, operation, kind, source,
                    plugin_id, started_at, status, outcome, degraded,
                    degradation_reasons_json, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["trace_id"],
                    payload["span_id"],
                    payload.get("parent_span_id"),
                    payload["operation"],
                    payload["kind"],
                    payload["source"],
                    payload.get("plugin_id"),
                    payload["started_at"],
                    payload["status"],
                    payload.get("outcome"),
                    int(payload.get("degraded", False)),
                    _encode_json(payload.get("degradation_reasons", [])),
                    _encode_json(payload.get("attributes", {})),
                ),
            )
            return
        if command.action == "span_patch":
            await db.execute(
                """
                UPDATE spans
                SET ended_at = ?, status = ?, outcome = ?, degraded = ?,
                    degradation_reasons_json = ?, attributes_json = ?
                WHERE trace_id = ? AND span_id = ? AND status = 'running'
                """,
                (
                    payload.get("ended_at"),
                    payload["status"],
                    payload.get("outcome"),
                    int(payload.get("degraded", False)),
                    _encode_json(payload.get("degradation_reasons", [])),
                    _encode_json(payload.get("attributes", {})),
                    payload["trace_id"],
                    payload["span_id"],
                ),
            )
            return
        if command.action == "event":
            await db.execute(
                """
                INSERT INTO events (
                    trace_id, span_id, event_index, name, occurred_at,
                    attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["trace_id"],
                    payload["span_id"],
                    payload["event_index"],
                    payload["name"],
                    payload["occurred_at"],
                    _encode_json(payload.get("attributes", {})),
                ),
            )
            return
        if command.action == "artifact_ref":
            await db.execute(
                """
                INSERT OR IGNORE INTO artifacts (
                    content_hash, media_type, logical_size, captured_size,
                    stored_size, codec, inline_body, storage_key, truncated,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["content_hash"],
                    payload["media_type"],
                    payload["logical_size"],
                    payload["captured_size"],
                    payload["stored_size"],
                    payload["codec"],
                    payload.get("inline_body"),
                    payload.get("storage_key"),
                    int(payload.get("truncated", False)),
                    payload["created_at"],
                ),
            )
            await db.execute(
                """
                INSERT INTO artifact_refs (
                    trace_id, span_id, ref_index, content_hash, role,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["trace_id"],
                    payload["span_id"],
                    payload["ref_index"],
                    payload["content_hash"],
                    payload["role"],
                    _encode_json(payload.get("metadata", {})),
                ),
            )
            return
        if command.action == "link":
            await db.execute(
                """
                INSERT INTO links (
                    trace_id, span_id, link_index, relation, target_trace_id,
                    target_span_id, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["trace_id"],
                    payload["span_id"],
                    payload["link_index"],
                    payload["relation"],
                    payload.get("target_trace_id"),
                    payload.get("target_span_id"),
                    _encode_json(payload.get("attributes", {})),
                ),
            )
            return
        raise TraceStorageError(f"unknown trace storage action: {command.action}")

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        """Return a trace detail tree without eagerly loading artifact bodies."""

        async with self._read_lock:
            db = self._require_read_db()
            begin_cursor = await db.execute("BEGIN")
            await begin_cursor.close()
            try:
                trace = await _fetch_one(
                    db, "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
                )
                if trace is None:
                    raise TraceNotFoundError(trace_id)
                spans = await _fetch_all(
                    db,
                    "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at, span_id",
                    (trace_id,),
                )
                events = await _fetch_all(
                    db,
                    """
                    SELECT * FROM events WHERE trace_id = ?
                    ORDER BY occurred_at, span_id, event_index
                    """,
                    (trace_id,),
                )
                artifact_refs = await _fetch_all(
                    db,
                    """
                    SELECT r.*, a.media_type, a.logical_size, a.captured_size,
                           a.stored_size, a.codec, a.truncated, a.storage_key
                    FROM artifact_refs AS r
                    JOIN artifacts AS a ON a.content_hash = r.content_hash
                    WHERE r.trace_id = ?
                    ORDER BY r.span_id, r.ref_index
                    """,
                    (trace_id,),
                )
                links = await _fetch_all(
                    db,
                    "SELECT * FROM links WHERE trace_id = ? ORDER BY span_id, link_index",
                    (trace_id,),
                )
            except Exception:
                await db.rollback()
                raise
            else:
                await db.commit()
        return {
            "trace": _decode_trace_row(trace),
            "spans": [_decode_span_row(row) for row in spans],
            "events": [_decode_event_row(row) for row in events],
            "artifact_refs": [
                _decode_artifact_ref_row(row, self._artifact_status(row))
                for row in artifact_refs
            ],
            "links": [_decode_link_row(row) for row in links],
        }

    async def list_traces(
        self,
        *,
        limit: int = 50,
        before_ended_at: float | None = None,
        before_trace_id: str | None = None,
        status: str | None = None,
        operation: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        plugin_id: str | None = None,
        degraded: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Return generic Trace summaries in stable newest-first order.

        The summary counts are scoped to each Trace.  ``artifact_count`` is
        the number of Artifact references in that Trace, rather than the
        number of unique content-addressed objects shared across all Traces.
        """

        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("t.status = ?")
            values.append(status)
        if operation:
            clauses.append("t.operation = ?")
            values.append(operation)
        if source:
            clauses.append("t.source = ?")
            values.append(source)
        if kind:
            clauses.append("t.kind = ?")
            values.append(kind)
        if plugin_id:
            clauses.append("t.plugin_id = ?")
            values.append(plugin_id)
        if degraded is not None:
            clauses.append("t.degraded = ?")
            values.append(int(degraded))
        if before_ended_at is not None and before_trace_id:
            clauses.append(
                "(COALESCE(t.ended_at, t.started_at) < ? OR "
                "(COALESCE(t.ended_at, t.started_at) = ? AND t.trace_id < ?))"
            )
            values.extend([before_ended_at, before_ended_at, before_trace_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 200)))
        async with self._read_lock:
            rows = await _fetch_all(
                self._require_read_db(),
                f"""
                SELECT
                    t.*,
                    (
                        SELECT COUNT(*)
                        FROM spans AS s
                        WHERE s.trace_id = t.trace_id
                    ) AS span_count,
                    (
                        SELECT COUNT(*)
                        FROM events AS e
                        WHERE e.trace_id = t.trace_id
                    ) AS event_count,
                    (
                        SELECT COUNT(*)
                        FROM artifact_refs AS r
                        WHERE r.trace_id = t.trace_id
                    ) AS artifact_count,
                    (
                        SELECT COUNT(*)
                        FROM links AS l
                        WHERE l.trace_id = t.trace_id
                    ) AS link_count,
                    (
                        SELECT s.operation
                        FROM spans AS s
                        WHERE s.trace_id = t.trace_id AND s.status = 'running'
                        ORDER BY s.started_at DESC, s.span_id DESC
                        LIMIT 1
                    ) AS active_span_operation
                FROM traces AS t {where}
                ORDER BY COALESCE(t.ended_at, t.started_at) DESC, t.trace_id DESC
                LIMIT ?
                """,
                tuple(values),
            )
        return [_decode_trace_row(row) for row in rows]

    async def get_overview(self, now: float | None = None) -> dict[str, Any]:
        """Return the intentionally small trace overview used by the WebUI."""

        now = time.time() if now is None else now
        since = now - 24 * 60 * 60
        async with self._read_lock:
            row = await _fetch_one(
                self._require_read_db(),
                """
                SELECT
                    SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS traces_24h,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN ended_at >= ? AND status = 'error' THEN 1 ELSE 0 END)
                        AS errors_24h
                FROM traces
                """,
                (since, since),
            )
        return {
            "traces_24h": int(row["traces_24h"] or 0) if row else 0,
            "running": int(row["running"] or 0) if row else 0,
            "errors_24h": int(row["errors_24h"] or 0) if row else 0,
            "physical_size": await self.physical_size(),
        }

    async def get_artifact_body(
        self, content_hash: str
    ) -> tuple[bytes, dict[str, Any]]:
        """Load an artifact body lazily and report missing/corrupt storage safely."""

        async with self._read_lock:
            row = await _fetch_one(
                self._require_read_db(),
                "SELECT * FROM artifacts WHERE content_hash = ?",
                (content_hash,),
            )
        if row is None:
            raise TraceNotFoundError(content_hash)
        metadata = {
            "content_hash": row["content_hash"],
            "media_type": row["media_type"],
            "logical_size": row["logical_size"],
            "captured_size": row["captured_size"],
            "stored_size": row["stored_size"],
            "codec": row["codec"],
            "truncated": bool(row["truncated"]),
        }
        if row["inline_body"] is not None:
            return bytes(row["inline_body"]), metadata | {
                "artifact_status": "available"
            }
        storage_key = row["storage_key"]
        if not storage_key:
            return b"", metadata | {"artifact_status": "missing"}
        path = self.objects_path / storage_key
        try:
            body = await asyncio.to_thread(path.read_bytes)
            if row["codec"] == "zlib":
                body = zlib.decompress(body)
        except FileNotFoundError:
            return b"", metadata | {"artifact_status": "missing"}
        except (OSError, zlib.error):
            return b"", metadata | {"artifact_status": "corrupt"}
        return body, metadata | {"artifact_status": "available"}

    async def delete_trace(self, trace_id: str) -> None:
        """Delete one terminal trace without interleaving writer transactions."""

        async with self._mutation_lock:
            await self._delete_trace(trace_id)

    async def _delete_trace(self, trace_id: str) -> None:
        """Delete one terminal trace and only its no-longer-referenced artifacts."""

        db = self._require_db()
        trace = await _fetch_one(
            db,
            "SELECT status FROM traces WHERE trace_id = ?",
            (trace_id,),
        )
        if trace is None:
            raise TraceNotFoundError(trace_id)
        if trace["status"] == "running":
            raise TraceDeleteConflictError(trace_id)
        hashes = await _fetch_all(
            db,
            "SELECT DISTINCT content_hash FROM artifact_refs WHERE trace_id = ?",
            (trace_id,),
        )
        await db.execute("BEGIN")
        try:
            await db.execute("DELETE FROM traces WHERE trace_id = ?", (trace_id,))
            orphan_rows: list[aiosqlite.Row] = []
            for hash_row in hashes:
                content_hash = hash_row["content_hash"]
                count_row = await _fetch_one(
                    db,
                    "SELECT COUNT(*) AS count FROM artifact_refs WHERE content_hash = ?",
                    (content_hash,),
                )
                if count_row and count_row["count"] == 0:
                    artifact = await _fetch_one(
                        db,
                        "SELECT storage_key FROM artifacts WHERE content_hash = ?",
                        (content_hash,),
                    )
                    if artifact:
                        orphan_rows.append(artifact)
                    await db.execute(
                        "DELETE FROM artifacts WHERE content_hash = ?",
                        (content_hash,),
                    )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        for artifact in orphan_rows:
            storage_key = artifact["storage_key"]
            if storage_key:
                try:
                    await asyncio.to_thread((self.objects_path / storage_key).unlink)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    async def clear_terminal_traces(self) -> int:
        """Delete every terminal trace while preserving running traces."""

        async with self._mutation_lock:
            db = self._require_db()
            placeholders = ",".join("?" for _ in TERMINAL_TRACE_STATUSES)
            rows = await _fetch_all(
                db,
                f"SELECT trace_id FROM traces WHERE status IN ({placeholders})",
                tuple(TERMINAL_TRACE_STATUSES),
            )
            for row in rows:
                await self._delete_trace(row["trace_id"])
            return len(rows)

    async def physical_size(self) -> int:
        """Return the physical size of database, WAL/SHM, objects, and tmp."""

        return await asyncio.to_thread(_directory_size, self.root)

    async def cleanup(
        self, *, retention_days: int, max_bytes: int, target_bytes: int
    ) -> dict[str, int]:
        """Serialize age/capacity cleanup with all other trace mutations."""

        async with self._mutation_lock:
            return await self._cleanup(
                retention_days=retention_days,
                max_bytes=max_bytes,
                target_bytes=target_bytes,
            )

    async def _cleanup(
        self, *, retention_days: int, max_bytes: int, target_bytes: int
    ) -> dict[str, int]:
        """Apply age and capacity retention without deleting running traces.

        Args:
            retention_days: Maximum age for terminal traces.
            max_bytes: Physical trace directory capacity threshold.
            target_bytes: Physical target after capacity cleanup.

        Returns:
            Counts and physical sizes before and after cleanup.
        """

        db = self._require_db()
        deleted = 0
        orphan_objects_removed = 0
        cutoff = time.time() - max(0, retention_days) * 24 * 60 * 60
        placeholders = ",".join("?" for _ in TERMINAL_TRACE_STATUSES)
        expired = await _fetch_all(
            db,
            f"""
            SELECT trace_id FROM traces
            WHERE status IN ({placeholders}) AND ended_at IS NOT NULL AND ended_at < ?
            ORDER BY ended_at ASC, trace_id ASC
            """,
            (*TERMINAL_TRACE_STATUSES, cutoff),
        )
        for row in expired:
            await self._delete_trace(row["trace_id"])
            deleted += 1
        before_capacity_cleanup = await self.physical_size()
        if before_capacity_cleanup > max_bytes:
            orphan_objects_removed = await self._clean_orphan_objects()
            before_capacity_cleanup = await self.physical_size()
        if before_capacity_cleanup > max_bytes:
            # A WebUI detail request can keep a WAL snapshot open.  Do not begin
            # capacity deletion unless its checkpoint can complete; otherwise a
            # stale reader prevents WAL shrinkage and turns capacity cleanup into
            # history deletion with no physical storage gain.
            if await self._checkpoint_wal():
                current_size = await self.physical_size()
                if current_size > max_bytes:
                    candidates = await _fetch_all(
                        db,
                        f"""
                        SELECT trace_id FROM traces
                        WHERE status IN ({placeholders})
                        ORDER BY ended_at ASC, trace_id ASC
                        """,
                        tuple(TERMINAL_TRACE_STATUSES),
                    )
                    candidate_index = 0
                    while current_size > target_bytes and candidate_index < len(
                        candidates
                    ):
                        batch, candidate_index = await self._select_capacity_batch(
                            candidates,
                            candidate_index,
                            bytes_to_reclaim=current_size - target_bytes,
                        )
                        for row in batch:
                            await self._delete_trace(row["trace_id"])
                            deleted += 1

                        if not await self._checkpoint_and_vacuum():
                            # A reader may have started after the preflight check.
                            # Stop after this deliberately small batch rather than
                            # treating the still-live WAL as reclaimable storage.
                            break
                        measured_size = await self.physical_size()
                        if measured_size >= current_size:
                            # The remaining size is unreclaimable by deleting these
                            # traces (for example SQLite's minimum database footprint).
                            # Preserve the rest of history rather than deleting it with
                            # no physical storage benefit.
                            break
                        current_size = measured_size
        return {
            "deleted": deleted,
            "orphan_objects_removed": orphan_objects_removed,
            "before_capacity_cleanup": before_capacity_cleanup,
            "physical_size": await self.physical_size(),
        }

    async def _select_capacity_batch(
        self,
        candidates: list[aiosqlite.Row],
        start_index: int,
        *,
        bytes_to_reclaim: int,
    ) -> tuple[list[aiosqlite.Row], int]:
        """Select a conservative oldest-first capacity deletion batch.

        A trace's unshared Artifact bytes are a useful lower-risk estimate of
        its reclaimable storage.  Database-row overhead is not represented in
        that value, so apply a small per-trace floor.  This keeps a one-byte
        overage from deleting the entire bounded batch while still permitting a
        larger batch when the target deficit is substantial.
        """

        selected: list[aiosqlite.Row] = []
        estimated_reclaimed = 0
        candidate_index = start_index
        required = max(1, bytes_to_reclaim)
        while (
            candidate_index < len(candidates)
            and len(selected) < _CAPACITY_CLEANUP_BATCH_SIZE
        ):
            row = candidates[candidate_index]
            candidate_index += 1
            selected.append(row)
            estimate = await self._estimate_trace_reclaimable_bytes(row["trace_id"])
            estimated_reclaimed += max(
                estimate,
                _CAPACITY_CLEANUP_MIN_TRACE_ESTIMATE_BYTES,
            )
            if estimated_reclaimed >= required:
                break
        return selected, candidate_index

    async def _estimate_trace_reclaimable_bytes(self, trace_id: str) -> int:
        """Estimate bytes freed when deleting one trace's unshared Artifacts."""

        row = await _fetch_one(
            self._require_db(),
            """
            SELECT COALESCE(SUM(a.stored_size), 0) AS reclaimable_bytes
            FROM artifacts AS a
            WHERE EXISTS (
                SELECT 1
                FROM artifact_refs AS own_ref
                WHERE own_ref.trace_id = ?
                  AND own_ref.content_hash = a.content_hash
            )
              AND NOT EXISTS (
                SELECT 1
                FROM artifact_refs AS other_ref
                WHERE other_ref.trace_id != ?
                  AND other_ref.content_hash = a.content_hash
            )
            """,
            (trace_id, trace_id),
        )
        if row is None:
            return 0
        try:
            return max(0, int(row["reclaimable_bytes"] or 0))
        except (TypeError, ValueError):
            return 0

    async def _checkpoint_and_vacuum(self) -> bool:
        """Compact SQLite after a bounded capacity-cleanup batch.

        ``PRAGMA wal_checkpoint`` returns a result cursor.  SQLite refuses to
        run ``VACUUM`` while that statement remains active, so consume and close
        it explicitly before issuing the compaction command.
        """

        if not await self._checkpoint_wal():
            return False

        db = self._require_db()
        vacuum_cursor = await db.execute("VACUUM")
        try:
            await vacuum_cursor.fetchall()
        finally:
            await vacuum_cursor.close()
        # VACUUM itself can emit WAL frames.  Checkpoint once more before the
        # caller measures physical size, otherwise the transient WAL growth
        # looks like failed reclamation and can distort the next cleanup pass.
        return await self._checkpoint_wal()

    async def _checkpoint_wal(self) -> bool:
        """Checkpoint WAL only when no active read snapshot prevents truncation."""

        db = self._require_db()
        checkpoint_cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            rows = await checkpoint_cursor.fetchall()
        finally:
            await checkpoint_cursor.close()
        if not rows or len(rows[0]) < 3:
            return False
        try:
            busy = int(rows[0][0])
            log_frames = int(rows[0][1])
            checkpointed_frames = int(rows[0][2])
        except (TypeError, ValueError):
            return False
        return busy == 0 and log_frames == checkpointed_frames

    async def clean_stale_tmp(self, older_than_seconds: float = 24 * 60 * 60) -> int:
        """Remove stale temporary CAS files without scanning all objects."""

        cutoff = time.time() - older_than_seconds
        removed = 0
        for path in self.tmp_path.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    async def clean_orphan_objects(self) -> int:
        """Remove CAS object files not referenced by the artifact index."""

        async with self._mutation_lock:
            return await self._clean_orphan_objects()

    async def _clean_orphan_objects(self) -> int:
        """Remove orphaned CAS objects while the caller owns the mutation lock."""

        db = self._require_db()
        rows = await _fetch_all(
            db,
            "SELECT storage_key FROM artifacts WHERE storage_key IS NOT NULL",
        )
        referenced = {
            str(row["storage_key"])
            for row in rows
            if isinstance(row["storage_key"], str)
        }
        return await asyncio.to_thread(
            _remove_orphan_objects,
            self.objects_path,
            referenced,
        )

    async def _prepare_artifact(self, payload: dict[str, Any]) -> None:
        """Prepare CAS metadata and atomically create a non-inline object if needed."""

        db = self._require_db()
        existing = await _fetch_one(
            db,
            "SELECT content_hash FROM artifacts WHERE content_hash = ?",
            (payload["content_hash"],),
        )
        if existing is not None:
            payload.setdefault("stored_size", 0)
            payload.setdefault("codec", "existing")
            return
        body = payload["body"]
        if not isinstance(body, bytes):
            raise TraceStorageError("artifact body must be bytes")
        if len(body) < INLINE_ARTIFACT_BYTES:
            payload["codec"] = "raw"
            payload["stored_size"] = len(body)
            payload["inline_body"] = body
            payload["storage_key"] = None
            return
        compressed = zlib.compress(body, level=6)
        storage_key = self._object_key(payload["content_hash"])
        object_path = self.objects_path / storage_key
        await asyncio.to_thread(self._write_object, object_path, compressed)
        payload["codec"] = "zlib"
        payload["stored_size"] = len(compressed)
        payload["inline_body"] = None
        payload["storage_key"] = storage_key

    def _object_key(self, content_hash: str) -> str:
        """Return a sharded relative path for an artifact content hash."""

        return f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.z"

    def _write_object(self, target: Path, content: bytes) -> None:
        """Write a CAS object through tmp and atomic rename on one filesystem."""

        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.tmp_path / f"{target.name}.{os.getpid()}.{time.time_ns()}"
        try:
            with temporary.open("xb") as file:
                file.write(content)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            except OSError:
                if not target.exists():
                    os.replace(temporary, target)
                    return
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _artifact_status(self, row: aiosqlite.Row) -> str:
        if row["storage_key"] is None:
            return "available"
        return (
            "available"
            if (self.objects_path / row["storage_key"]).is_file()
            else "missing"
        )

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise TraceStorageError("trace store is not initialized")
        return self._db

    def _require_read_db(self) -> aiosqlite.Connection:
        """Return the dedicated read-only SQLite connection."""

        if self._read_db is None:
            raise TraceStorageError("trace store is not initialized")
        return self._read_db


async def _fetch_one(
    db: aiosqlite.Connection,
    query: str,
    values: tuple[Any, ...] = (),
) -> aiosqlite.Row | None:
    cursor = await db.execute(query, values)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetch_all(
    db: aiosqlite.Connection,
    query: str,
    values: tuple[Any, ...] = (),
) -> list[aiosqlite.Row]:
    cursor = await db.execute(query, values)
    try:
        return await cursor.fetchall()
    finally:
        await cursor.close()


def _encode_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _decode_trace_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data["degraded"] = bool(data["degraded"])
    data["degradation_reasons"] = _decode_json(data.pop("degradation_reasons_json"), [])
    data["attributes"] = _decode_json(data.pop("attributes_json"), {})
    data["dropped"] = _decode_json(data.pop("dropped_json"), {})
    return data


def _decode_span_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data["degraded"] = bool(data["degraded"])
    data["degradation_reasons"] = _decode_json(data.pop("degradation_reasons_json"), [])
    data["attributes"] = _decode_json(data.pop("attributes_json"), {})
    return data


def _decode_event_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data["attributes"] = _decode_json(data.pop("attributes_json"), {})
    return data


def _decode_artifact_ref_row(
    row: aiosqlite.Row, artifact_status: str
) -> dict[str, Any]:
    data = dict(row)
    data["truncated"] = bool(data["truncated"])
    data["metadata"] = _decode_json(data.pop("metadata_json"), {})
    data["artifact_status"] = artifact_status
    return data


def _decode_link_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data["attributes"] = _decode_json(data.pop("attributes_json"), {})
    return data


def _directory_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _remove_orphan_objects(objects_path: Path, referenced: set[str]) -> int:
    """Delete CAS object files that have no row in the artifact index."""

    if not objects_path.exists():
        return 0
    removed = 0
    for path in objects_path.rglob("*"):
        try:
            if not path.is_file():
                continue
            storage_key = path.relative_to(objects_path).as_posix()
            if storage_key in referenced:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    for directory in sorted(
        (path for path in objects_path.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            continue
    return removed
