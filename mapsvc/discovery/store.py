"""SQLite catalogue of data sources found by the research agent."""

import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


SCHEMA_VERSION = 1
_JSON_FIELDS = ("roles", "coverage", "validation")
_FIELDS = (
    "id", "url", "title", "description", "publisher", "licence", "format",
    "geometry_kind", "key_namespace", "level", "roles", "units", "vintage",
    "coverage", "content_sha256", "status", "validation", "keywords",
    "query_text", "discovered_at", "validated_at", "last_used_at", "uses",
)
_STATUSES = {"active", "stale", "rejected"}
_GEOMETRIES = {"choropleth", "points", "lines"}
_KEYS = {"ISO3", "ISO3166_2", "FIPS", "NUTS", "coords", "name"}


def source_id(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


class DiscoveredSourceStore:
    def __init__(self, path: str | os.PathLike | None = None):
        if path is None:
            root = Path(os.environ.get(
                "MAPSVC_CACHE", Path(__file__).resolve().parents[2] / "cache"))
            path = root / "discovered.sqlite"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self.connection.execute(
                "SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] > SCHEMA_VERSION:
                raise RuntimeError(f"discovery database schema {row[0]} is newer than supported")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT,
                    description TEXT, publisher TEXT, licence TEXT, format TEXT,
                    geometry_kind TEXT CHECK (geometry_kind IS NULL OR geometry_kind IN
                        ('choropleth','points','lines')),
                    key_namespace TEXT CHECK (key_namespace IS NULL OR key_namespace IN
                        ('ISO3','ISO3166_2','FIPS','NUTS','coords','name')),
                    level TEXT, roles TEXT, units TEXT, vintage TEXT, coverage TEXT,
                    content_sha256 TEXT,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                        ('active','stale','rejected')),
                    validation TEXT, keywords TEXT, query_text TEXT,
                    discovered_at TEXT NOT NULL, validated_at TEXT,
                    last_used_at TEXT, uses INTEGER NOT NULL DEFAULT 0
                )
            """)
            self.connection.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
                    title, description, keywords, publisher, query_text,
                    content='sources', content_rowid='rowid')
            """)
            self.connection.executescript("""
                CREATE TRIGGER IF NOT EXISTS sources_ai AFTER INSERT ON sources BEGIN
                  INSERT INTO sources_fts(rowid,title,description,keywords,publisher,query_text)
                  VALUES(new.rowid,new.title,new.description,new.keywords,new.publisher,new.query_text);
                END;
                CREATE TRIGGER IF NOT EXISTS sources_ad AFTER DELETE ON sources BEGIN
                  INSERT INTO sources_fts(sources_fts,rowid,title,description,keywords,publisher,query_text)
                  VALUES('delete',old.rowid,old.title,old.description,old.keywords,old.publisher,old.query_text);
                END;
                CREATE TRIGGER IF NOT EXISTS sources_au AFTER UPDATE ON sources BEGIN
                  INSERT INTO sources_fts(sources_fts,rowid,title,description,keywords,publisher,query_text)
                  VALUES('delete',old.rowid,old.title,old.description,old.keywords,old.publisher,old.query_text);
                  INSERT INTO sources_fts(rowid,title,description,keywords,publisher,query_text)
                  VALUES(new.rowid,new.title,new.description,new.keywords,new.publisher,new.query_text);
                END;
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS negatives (
                    query_norm TEXT PRIMARY KEY, searched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, report TEXT NOT NULL
                )
            """)
            if row is not None and row[0] < SCHEMA_VERSION:
                # Future migrations are applied above in ascending version
                # order, then this single-row marker advances atomically.
                self.connection.execute(
                    "UPDATE schema_version SET version=?", (SCHEMA_VERSION,))

    def add_or_update(self, record: dict) -> dict:
        if not record.get("url"):
            raise ValueError("a discovered source requires url")
        values = dict(record)
        values.setdefault("id", source_id(values["url"]))
        values.setdefault("status", "active")
        values.setdefault("discovered_at", _now())
        values.setdefault("uses", 0)
        if values["status"] not in _STATUSES:
            raise ValueError(f"invalid source status {values['status']!r}")
        if values.get("geometry_kind") not in _GEOMETRIES | {None}:
            raise ValueError(f"invalid geometry_kind {values['geometry_kind']!r}")
        if values.get("key_namespace") not in _KEYS | {None}:
            raise ValueError(f"invalid key_namespace {values['key_namespace']!r}")
        for field in _JSON_FIELDS:
            if field in values and values[field] is not None and not isinstance(values[field], str):
                values[field] = json.dumps(values[field], sort_keys=True, separators=(",", ":"))
        present = [field for field in _FIELDS if field in values]
        columns = ",".join(present)
        placeholders = ",".join("?" for _ in present)
        updates = ",".join(f"{field}=excluded.{field}" for field in present
                           if field not in {"id", "discovered_at", "uses", "last_used_at"})
        sql = (f"INSERT INTO sources ({columns}) VALUES ({placeholders}) "
               f"ON CONFLICT(id) DO UPDATE SET {updates}")
        with self.connection:
            self.connection.execute(sql, [values[field] for field in present])
        return self.get(values["id"])

    def get(self, id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM sources WHERE id=?", (id,)).fetchone()
        return self._decode(row) if row else None

    def mark_used(self, id: str) -> dict | None:
        with self.connection:
            self.connection.execute(
                "UPDATE sources SET uses=uses+1,last_used_at=? WHERE id=?", (_now(), id))
        return self.get(id)

    def set_status(self, id: str, status: str, reason: str) -> dict | None:
        if status not in _STATUSES:
            raise ValueError(f"invalid source status {status!r}")
        current = self.get(id)
        if current is None:
            return None
        validation = dict(current.get("validation") or {})
        validation["status_reason"] = reason
        with self.connection:
            self.connection.execute(
                "UPDATE sources SET status=?,validation=? WHERE id=?",
                (status, json.dumps(validation, sort_keys=True, separators=(",", ":")), id))
        return self.get(id)

    def search(self, text: str, *, key_namespace: str | None = None,
               level: str | None = None, geometry_kind: str | None = None,
               status: str = "active", limit: int = 10) -> list[dict]:
        if limit <= 0:
            return []
        tokens = re.findall(r"[\w-]+", text, flags=re.UNICODE)
        match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        clauses = ["s.status=?"]
        params: list = [status]
        for column, value in (("key_namespace", key_namespace), ("level", level),
                              ("geometry_kind", geometry_kind)):
            if value is not None:
                clauses.append(f"s.{column}=?")
                params.append(value)
        where = " AND ".join(clauses)
        if match:
            rows = self.connection.execute(
                f"""SELECT s.*, bm25(sources_fts) AS rank FROM sources_fts
                    JOIN sources s ON s.rowid=sources_fts.rowid
                    WHERE sources_fts MATCH ? AND {where}
                    ORDER BY rank ASC, s.id ASC LIMIT ?""",
                [match, *params, limit]).fetchall()
        else:
            rows = self.connection.execute(
                f"SELECT s.*, 0.0 AS rank FROM sources s WHERE {where} "
                "ORDER BY s.id ASC LIMIT ?", [*params, limit]).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _normalise_query(query: str) -> str:
        return " ".join(query.casefold().split())

    def record_negative(self, query: str, report, *, days: int = 30) -> None:
        now = _datetime.datetime.now(_datetime.timezone.utc)
        expires = now + _datetime.timedelta(days=days)
        encoded = report if isinstance(report, str) else json.dumps(
            report, sort_keys=True, separators=(",", ":"))
        with self.connection:
            self.connection.execute("""
                INSERT INTO negatives(query_norm,searched_at,expires_at,report)
                VALUES(?,?,?,?) ON CONFLICT(query_norm) DO UPDATE SET
                searched_at=excluded.searched_at,expires_at=excluded.expires_at,
                report=excluded.report
            """, (self._normalise_query(query), now.isoformat(), expires.isoformat(), encoded))

    def recent_negative(self, query: str) -> dict | None:
        now = _now()
        row = self.connection.execute(
            "SELECT * FROM negatives WHERE query_norm=? AND expires_at>?",
            (self._normalise_query(query), now)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["report"] = json.loads(result["report"])
        except (TypeError, json.JSONDecodeError):
            pass
        return result

    @staticmethod
    def _decode(row) -> dict:
        result = dict(row)
        result.pop("rank", None)
        for field in _JSON_FIELDS:
            if result.get(field) is not None:
                try:
                    result[field] = json.loads(result[field])
                except (TypeError, json.JSONDecodeError):
                    pass
        return result


# Short, natural name for callers that import ``mapsvc.discovery.store.Store``.
Store = DiscoveredSourceStore
DiscoveryStore = DiscoveredSourceStore
SourceStore = DiscoveredSourceStore
