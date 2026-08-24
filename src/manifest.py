"""
Tracks every document the agent has ever seen, so re-runs (every 24h)
never re-download or re-classify the same document twice, and the
100-new-document cap only counts genuinely new documents.

SQLite was chosen deliberately over a JSON/CSV log: this file is written
to incrementally on every run, and SQLite gives us atomic writes and a
UNIQUE constraint for dedup instead of hand-rolled "read whole file,
check membership, rewrite whole file" logic.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL UNIQUE,
    source_page     TEXT,
    title           TEXT,
    file_hash       TEXT,
    raw_path        TEXT,
    extraction_method TEXT,   -- 'native' | 'ocr' | 'failed'
    category        TEXT,
    final_path      TEXT,
    status          TEXT NOT NULL DEFAULT 'discovered',
                    -- discovered -> downloaded -> extracted -> classified -> organized
                    -- or: failed_download / failed_extract / failed_classify
    error_message   TEXT,
    discovered_at   TEXT NOT NULL,
    downloaded_at   TEXT,
    classified_at   TEXT,
    organized_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(file_hash);
"""


class Manifest:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # A row in one of these states represents work that did NOT complete for a
    # transient reason (site 403/timeout/network blip). Those must be retried on
    # the next run, otherwise the very first failed attempt permanently
    # blacklists a document: `already_known` would report "seen it" forever and
    # the downloader would skip it, mistaking "we tried and failed" for "done".
    RETRYABLE_STATUSES = ("failed_download", "failed_extract", "failed_classify", "failed_organize")

    def already_known(self, source_url: str) -> bool:
        """
        True only if this URL has been seen AND is not sitting in a retryable
        failure state. Retryable failures deliberately report False so the
        next run picks them up again.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM documents WHERE source_url = ?", (source_url,)
            ).fetchone()
            if row is None:
                return False
            return row["status"] not in self.RETRYABLE_STATUSES

    def reset_for_retry(self, source_url: str) -> None:
        """Clears a previous failure so the row can go cleanly through the pipeline again."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE documents
                   SET status = 'discovered', error_message = NULL
                   WHERE source_url = ? AND status IN (%s)"""
                % ",".join("?" * len(self.RETRYABLE_STATUSES)),
                (source_url, *self.RETRYABLE_STATUSES),
            )

    def retryable_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM documents WHERE status IN (%s)"
                % ",".join("?" * len(self.RETRYABLE_STATUSES)),
                self.RETRYABLE_STATUSES,
            ).fetchone()
            return row["n"]

    def hash_already_downloaded(self, file_hash: str, source_url: str = "") -> bool:
        """
        Catches the same PDF reachable via two different URLs.

        `source_url` is excluded from the comparison: when a document is
        retried after a later-stage failure it is re-downloaded and will
        naturally match the hash stored on its OWN row. Without this exclusion
        every retry would be misfiled as a duplicate and could never complete.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM documents "
                "WHERE file_hash = ? AND file_hash IS NOT NULL AND source_url != ?",
                (file_hash, source_url),
            ).fetchone()
            return row is not None

    def record_discovered(self, source_url: str, source_page: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO documents
                   (source_url, source_page, title, status, discovered_at)
                   VALUES (?, ?, ?, 'discovered', ?)""",
                (source_url, source_page, title, datetime.now().isoformat()),
            )

    def record_downloaded(self, source_url: str, raw_path: str, file_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE documents
                   SET raw_path = ?, file_hash = ?, status = 'downloaded', downloaded_at = ?
                   WHERE source_url = ?""",
                (raw_path, file_hash, datetime.now().isoformat(), source_url),
            )

    def record_extracted(self, source_url: str, extraction_method: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE documents SET extraction_method = ?, status = 'extracted'
                   WHERE source_url = ?""",
                (extraction_method, source_url),
            )

    def record_classified(self, source_url: str, category: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE documents
                   SET category = ?, status = 'classified', classified_at = ?
                   WHERE source_url = ?""",
                (category, datetime.now().isoformat(), source_url),
            )

    def record_organized(self, source_url: str, final_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE documents
                   SET final_path = ?, status = 'organized', organized_at = ?
                   WHERE source_url = ?""",
                (final_path, datetime.now().isoformat(), source_url),
            )

    def record_failure(self, source_url: str, status: str, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE documents SET status = ?, error_message = ? WHERE source_url = ?""",
                (status, error_message[:2000], source_url),
            )

    def run_summary(self, since_iso: str) -> dict:
        """Category/status counts for documents touched since a given timestamp."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, category, COUNT(*) as n FROM documents "
                "WHERE discovered_at >= ? GROUP BY status, category",
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows]
