"""SQLite persistence for releases, known tracks and source state."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .names import replace_surrogates

STATUSES = ("processing", "pending", "approved", "declined", "empty", "failed")
_UPDATABLE = {"status", "decided_at", "staging_path", "library_path", "report", "error"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL,
    staging_path TEXT,
    library_path TEXT,
    report TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status, created_at);
CREATE TABLE IF NOT EXISTS tracks (
    hash TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    relpath TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_release ON tracks(release_id);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    started_ts REAL NOT NULL,
    ended_ts REAL NOT NULL,
    start_lat REAL NOT NULL,
    start_lon REAL NOT NULL,
    end_lat REAL NOT NULL,
    end_lon REAL NOT NULL,
    distance_m REAL NOT NULL,
    duration_s INTEGER NOT NULL,
    energy_kwh REAL,
    soc_start REAL,
    soc_end REAL,
    avg_consumption_kwh_per_100km REAL,
    ext_temp_c REAL,
    shape TEXT,
    source TEXT NOT NULL DEFAULT 'car-app',
    vehicle TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_started ON trips(started_ts);
CREATE INDEX IF NOT EXISTS idx_trips_ended ON trips(ended_ts);
CREATE TABLE IF NOT EXISTS ev_settings (
    key TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_samples (
    trip_id TEXT NOT NULL,
    t TEXT NOT NULL,
    t_ts REAL NOT NULL,
    soc REAL,
    power_kw REAL,
    speed_kmh REAL,
    lat REAL,
    lon REAL,
    PRIMARY KEY (trip_id, t_ts)
);
"""

TRIP_FIELDS = (
    "id", "started_at", "ended_at", "start_lat", "start_lon", "end_lat", "end_lon", "distance_m",
    "duration_s", "energy_kwh", "soc_start", "soc_end", "avg_consumption_kwh_per_100km", "ext_temp_c",
    "shape", "source", "vehicle",
)
SAMPLE_FIELDS = ("t", "soc", "power_kw", "speed_kmh", "lat", "lon")

_COLUMNS = "id, name, source_hash, status, created_at, decided_at, staging_path, library_path, report, error"


@dataclass
class Release:
    """A release row."""

    id: str
    name: str
    source_hash: str
    status: str
    created_at: float
    decided_at: float | None = None
    staging_path: str | None = None
    library_path: str | None = None
    report: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _row_to_release(row: tuple) -> Release:
    try:
        report = json.loads(row[8]) if row[8] else {}
    except json.JSONDecodeError:
        report = {}
    return Release(
        id=row[0], name=row[1], source_hash=row[2], status=row[3], created_at=row[4],
        decided_at=row[5], staging_path=row[6], library_path=row[7], report=report, error=row[9],
    )


class Database:
    """Synchronous sqlite wrapper; use only from the event-loop thread."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def get_by_source_hash(self, source_hash: str) -> Release | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM releases WHERE source_hash = ?", (source_hash,)
        ).fetchone()
        return _row_to_release(row) if row else None

    def create_release(self, id: str, name: str, source_hash: str, staging_path: str) -> Release:
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO releases (id, name, source_hash, status, created_at, staging_path, report) "
                "VALUES (?, ?, ?, 'processing', ?, ?, '{}')",
                (id, name, source_hash, now, staging_path),
            )
        return Release(id=id, name=name, source_hash=source_hash, status="processing",
                       created_at=now, staging_path=staging_path)

    def get(self, id: str) -> Release | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM releases WHERE id = ?", (id,)).fetchone()
        return _row_to_release(row) if row else None

    def update(self, id: str, **fields: Any) -> None:
        """Update allowed columns of a release; ``report`` is a dict stored as JSON."""
        if not fields:
            return
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update fields: {', '.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields['status']!r}")
        if "report" in fields:
            fields["report"] = json.dumps(fields["report"])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._conn:
            self._conn.execute(f"UPDATE releases SET {assignments} WHERE id = ?", (*fields.values(), id))

    def list(self, statuses: Iterable[str] | None = None, limit: int = 100) -> list[Release]:
        """Return releases newest first, optionally filtered by status."""
        query = f"SELECT {_COLUMNS} FROM releases"
        params: list[Any] = []
        if statuses is not None:
            wanted = list(statuses)
            if not wanted:
                return []
            query += f" WHERE status IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return [_row_to_release(r) for r in self._conn.execute(query, params).fetchall()]

    def delete(self, id: str) -> None:
        """Delete a release and its tracks."""
        with self._conn:
            self._conn.execute("DELETE FROM tracks WHERE release_id = ?", (id,))
            self._conn.execute("DELETE FROM releases WHERE id = ?", (id,))

    def count(self, statuses: Iterable[str] | None = None) -> int:
        """Number of releases, optionally only those with one of ``statuses``."""
        query = "SELECT COUNT(*) FROM releases"
        params: list[Any] = []
        if statuses is not None:
            wanted = list(statuses)
            if not wanted:
                return 0
            query += f" WHERE status IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        return int(self._conn.execute(query, params).fetchone()[0])

    def add_tracks(self, release_id: str, tracks: Iterable[tuple[str, str]]) -> None:
        """Record ``(sha256, relpath)`` pairs; already-known hashes are ignored.

        Only ``has_track(hash)`` is ever queried, so a relpath is informational: lone surrogates
        (undecodable file-name bytes, which sqlite cannot store) become U+FFFD instead of aborting
        the whole batch.
        """
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO tracks (hash, release_id, relpath) VALUES (?, ?, ?)",
                [(h, release_id, replace_surrogates(rel)) for h, rel in tracks],
            )

    def delete_tracks_under(self, release_id: str, relpath: str) -> int:
        """Forget the tracks of ``release_id`` whose relpath is ``relpath`` or lies below it.

        ``relpath`` is a ``/``-separated path; ``a/b`` matches ``a/b`` and ``a/b/c.mp3`` but never
        ``a/bc.mp3``. Returns the number of tracks forgotten.
        """
        relpath = replace_surrogates(relpath).strip("/")
        prefix = relpath + "/"
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM tracks WHERE release_id = ? AND (relpath = ? OR substr(relpath, 1, ?) = ?)",
                (release_id, relpath, len(prefix), prefix),
            )
        return cur.rowcount

    def forget_source_hash(self, id: str) -> None:
        """Give a release a placeholder source hash, so the same source can be imported again."""
        with self._conn:
            self._conn.execute("UPDATE releases SET source_hash = ? WHERE id = ?", (f"forgotten:{id}", id))

    def has_track(self, sha256: str) -> bool:
        return self._conn.execute("SELECT 1 FROM tracks WHERE hash = ?", (sha256,)).fetchone() is not None

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------ EV settings (car app)
    def get_ev_settings(self, key: str = "ev") -> tuple[str, str, int] | None:
        """``(json text, updated_at, revision)`` or None when nothing was stored yet."""
        row = self._conn.execute(
            "SELECT json, updated_at, revision FROM ev_settings WHERE key = ?", (key,)
        ).fetchone()
        return (row[0], row[1], int(row[2])) if row else None

    def put_ev_settings(
        self, key: str, json_text: str, base_revision: int, updated_at: str
    ) -> tuple[bool, tuple[str, str, int] | None]:
        """Store when ``base_revision`` is the stored revision (0 = nothing stored). ``(ok, current document)``."""
        current = self.get_ev_settings(key)
        if (current[2] if current else 0) != base_revision:
            return False, current
        revision = base_revision + 1
        with self._conn:
            self._conn.execute(
                "INSERT INTO ev_settings (key, json, updated_at, revision) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at, "
                "revision = excluded.revision",
                (key, json_text, updated_at, revision),
            )
        return True, (json_text, updated_at, revision)

    # ------------------------------------------------------------------ trips (car app uploads)
    def upsert_trip(self, trip: dict[str, Any], started_ts: float, ended_ts: float) -> bool:
        """Insert or replace one validated trip by id; True when it was new, False when it replaced one."""
        now = time.time()
        existed = self._conn.execute("SELECT 1 FROM trips WHERE id = ?", (trip["id"],)).fetchone() is not None
        cols = ", ".join(TRIP_FIELDS)
        marks = ", ".join("?" * len(TRIP_FIELDS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in TRIP_FIELDS if c != "id")
        with self._conn:
            self._conn.execute(
                f"INSERT INTO trips ({cols}, started_ts, ended_ts, created_at, updated_at) "
                f"VALUES ({marks}, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET {updates}, "
                "started_ts = excluded.started_ts, ended_ts = excluded.ended_ts, updated_at = excluded.updated_at",
                (*(trip[c] for c in TRIP_FIELDS), started_ts, ended_ts, now, now),
            )
        return not existed

    def get_trip(self, id: str) -> dict[str, Any] | None:
        row = self._conn.execute(f"SELECT {', '.join(TRIP_FIELDS)} FROM trips WHERE id = ?", (id,)).fetchone()
        return dict(zip(TRIP_FIELDS, row)) if row else None

    def list_trips(
        self, since_ts: float | None = None, before_ts: float | None = None, limit: int = 100,
        with_shape: bool = True,
    ) -> list[dict[str, Any]]:
        """Trips newest first (by start), optionally only those started after ``since_ts`` / before ``before_ts``."""
        cols = [c for c in TRIP_FIELDS if with_shape or c != "shape"]
        where: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            where.append("started_ts > ?")
            params.append(since_ts)
        if before_ts is not None:
            where.append("started_ts < ?")
            params.append(before_ts)
        query = f"SELECT {', '.join(cols)} FROM trips"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY started_ts DESC, id LIMIT ?"
        params.append(limit)
        return [dict(zip(cols, r)) for r in self._conn.execute(query, params).fetchall()]

    def query_trips(
        self, since_ts: float | None = None, until_ts: float | None = None, limit: int = 50, offset: int = 0,
        with_shape: bool = False,
    ) -> list[dict[str, Any]]:
        """Trips started in ``[since_ts, until_ts)``, newest first, one page (web UI)."""
        cols = [c for c in TRIP_FIELDS if with_shape or c != "shape"]
        # the track itself is not loaded for lists; only whether there is one
        select = ", ".join(cols) + ", (shape IS NOT NULL AND shape != '') AS has_shape"
        where: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            where.append("started_ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            where.append("started_ts < ?")
            params.append(until_ts)
        query = f"SELECT {select} FROM trips"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY started_ts DESC, id LIMIT ? OFFSET ?"
        params += [limit, offset]
        out = []
        for r in self._conn.execute(query, params).fetchall():
            row = dict(zip([*cols, "has_shape"], r))
            row["has_shape"] = bool(row["has_shape"])
            out.append(row)
        return out

    def trip_months(self) -> list[str]:
        """UTC months (``YYYY-MM``) that have trips, newest first."""
        rows = self._conn.execute(
            "SELECT DISTINCT strftime('%Y-%m', started_ts, 'unixepoch') AS m FROM trips ORDER BY m DESC"
        ).fetchall()
        return [r[0] for r in rows]

    def delete_trip(self, id: str) -> bool:
        """Delete a trip and its samples; False when there was no such trip."""
        with self._conn:
            self._conn.execute("DELETE FROM trip_samples WHERE trip_id = ?", (id,))
            cur = self._conn.execute("DELETE FROM trips WHERE id = ?", (id,))
        return cur.rowcount > 0

    def add_trip_samples(self, trip_id: str, samples: list[tuple[dict[str, Any], float]]) -> int:
        """Store ``(sample, t_ts)`` pairs of a trip; a sample at the same instant is replaced. Returns the count."""
        cols = ", ".join(SAMPLE_FIELDS)
        with self._conn:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO trip_samples (trip_id, {cols}, t_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(trip_id, *(s[c] for c in SAMPLE_FIELDS), ts) for s, ts in samples],
            )
        return len(samples)

    def list_trip_samples(self, trip_id: str, limit: int = 20000) -> list[dict[str, Any]]:
        cols = ", ".join(SAMPLE_FIELDS)
        rows = self._conn.execute(
            f"SELECT {cols} FROM trip_samples WHERE trip_id = ? ORDER BY t_ts LIMIT ?", (trip_id, limit)
        ).fetchall()
        return [dict(zip(SAMPLE_FIELDS, r)) for r in rows]

    def trip_totals(self, since_ts: float | None = None, until_ts: float | None = None) -> dict[str, Any]:
        """Sums over trips started in ``[since_ts, until_ts)``: count, metres, kWh and the metres of trips that have kWh."""
        where: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            where.append("started_ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            where.append("started_ts < ?")
            params.append(until_ts)
        query = (
            "SELECT COUNT(*), COALESCE(SUM(distance_m), 0), COALESCE(SUM(energy_kwh), 0), "
            "COALESCE(SUM(CASE WHEN energy_kwh IS NOT NULL THEN distance_m END), 0) FROM trips"
        )
        if where:
            query += " WHERE " + " AND ".join(where)
        row = self._conn.execute(query, params).fetchone()
        return {"trips": row[0], "distance_m": row[1], "energy_kwh": row[2], "energy_distance_m": row[3]}

    def last_trip(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {', '.join(TRIP_FIELDS)} FROM trips ORDER BY ended_ts DESC, id LIMIT 1"
        ).fetchone()
        return dict(zip(TRIP_FIELDS, row)) if row else None
