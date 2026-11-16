"""SQLite connection, schema and transaction helpers.

All connection handling is deliberately small: one short-lived connection per
request, ``isolation_level=None`` so that transactions are explicit, WAL mode
for concurrent readers, and a busy timeout so that writers queue instead of
failing immediately under contention.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    measurement_no TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sample', 'blank', 'spike')),
    sample_no INTEGER CHECK (sample_no IS NULL OR sample_no BETWEEN 1 AND 100),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    value_mg_l REAL NOT NULL CHECK (value_mg_l >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, measurement_no, revision),
    CHECK (
        (kind = 'sample' AND sample_no IS NOT NULL)
        OR (kind IN ('blank', 'spike') AND sample_no IS NULL)
    )
);

-- Revision history means several rows can share a measurement point; the
-- service enforces one measurement number per point and one blank/spike per
-- batch when a point is first recorded.
CREATE INDEX IF NOT EXISTS ix_measurements_point
    ON measurements (batch_id, kind, sample_no);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    published_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current', 'expired')),
    expired_at TEXT,
    blank_measurement_no TEXT NOT NULL,
    blank_revision INTEGER NOT NULL,
    blank_value_mg_l REAL NOT NULL,
    spike_measurement_no TEXT NOT NULL,
    spike_revision INTEGER NOT NULL,
    spike_value_mg_l REAL NOT NULL,
    spike_added_mg_l REAL NOT NULL,
    corrected_spike_mg_l REAL NOT NULL,
    recovery_percent REAL NOT NULL,
    recovery_min_percent REAL NOT NULL,
    recovery_max_percent REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    CHECK (status = 'current' OR expired_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS publication_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    sample_no INTEGER NOT NULL,
    measurement_no TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    source_value_mg_l REAL NOT NULL,
    concentration_mg_l REAL,
    reason TEXT,
    note TEXT,
    UNIQUE (publication_id, sample_no),
    CHECK (
        (concentration_mg_l IS NULL AND reason IS NOT NULL)
        OR (concentration_mg_l IS NOT NULL AND reason IS NULL)
    )
);

-- A batch can never expose two current reports at the same time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_publications_one_current
    ON publications (batch_id)
    WHERE status = 'current';

CREATE INDEX IF NOT EXISTS ix_publications_batch
    ON publications (batch_id, id);
CREATE INDEX IF NOT EXISTS ix_measurements_batch
    ON measurements (batch_id, measurement_no, revision);
"""


def connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # WAL is a persistent property of the database file, so it is set once in
    # ``init_db`` and not re-asserted (and re-locked) on every request.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db(db_path: str | os.PathLike[str]) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        # ``executescript`` commits any pending transaction before it runs, so
        # the transaction control has to live inside the script itself.
        conn.executescript(
            "PRAGMA journal_mode = WAL;\nBEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;"
        )
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def transaction(
    conn: sqlite3.Connection, *, immediate: bool = True
) -> Iterator[sqlite3.Connection]:
    """Run a unit of work atomically.

    ``BEGIN IMMEDIATE`` is used for every write.  It takes the SQLite write
    lock before any read, which serialises publishing with corrections and
    guarantees that a report is built from one stable set of revisions.
    """

    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
