"""Локальная БД на SQLite (stdlib, ничего устанавливать не нужно).

Хранит объявления (дедупликация по id Авито), источники, историю цен,
события капчи и накопленные картинки капчи для дообучения локальной модели.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .models import Listing, Source, SourceStatus
from .utils import MSK, content_hash

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL DEFAULT '',
    interval_sec      INTEGER NOT NULL DEFAULT 300,
    pages             INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'active',
    seed_existing     INTEGER NOT NULL DEFAULT 1,
    min_price         INTEGER,
    max_price         INTEGER,
    keywords_include  TEXT NOT NULL DEFAULT '',
    keywords_exclude  TEXT NOT NULL DEFAULT '',
    last_check_at     REAL,
    next_check_at     REAL NOT NULL DEFAULT 0,
    last_error        TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    found_total       INTEGER NOT NULL DEFAULT 0,
    notified_total    INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    item_id       TEXT PRIMARY KEY,
    source_id     INTEGER,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    price         INTEGER,
    price_text    TEXT NOT NULL DEFAULT '',
    currency      TEXT NOT NULL DEFAULT 'RUB',
    old_price     INTEGER,
    discount_pct  INTEGER,
    image_url     TEXT,
    images_json   TEXT NOT NULL DEFAULT '[]',
    description   TEXT,
    published_at  TEXT,
    published_raw TEXT,
    location      TEXT,
    seller_name   TEXT,
    seller_type   TEXT,
    params        TEXT,
    price_source  TEXT,
    content_hash  TEXT,
    raw_json      TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    notified_at   REAL,
    notify_error  TEXT,
    enriched_at   REAL,
    seen_count    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_listings_notified ON listings(notified_at);
CREATE INDEX IF NOT EXISTS idx_listings_source   ON listings(source_id, first_seen_at DESC);

CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    price      INTEGER,
    changed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_item ON price_history(item_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS captcha_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER,
    variant     TEXT NOT NULL,
    url         TEXT,
    method      TEXT,
    solved      INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    screenshot  TEXT,
    created_at  REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS captcha_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL,
    label      TEXT,
    variant    TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'new',
    created_at REAL NOT NULL,
    sent_at    REAL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON telegram_queue(sent_at, created_at);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------- meta ----------
    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------- sources ----------
    def add_source(self, url: str, *, name: str = "", interval_sec: int = 300, pages: int = 1,
                   seed_existing: bool = True, min_price: int | None = None,
                   max_price: int | None = None, keywords_include: list[str] | None = None,
                   keywords_exclude: list[str] | None = None) -> Source:
        now = time.time()
        with self.tx() as c:
            c.execute(
                """INSERT INTO sources(url,name,interval_sec,pages,seed_existing,min_price,max_price,
                                       keywords_include,keywords_exclude,created_at,next_check_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                     name=excluded.name, interval_sec=excluded.interval_sec, pages=excluded.pages""",
                (url, name, interval_sec, pages, int(seed_existing), min_price, max_price,
                 ",".join(keywords_include or []), ",".join(keywords_exclude or []), now, now),
            )
        row = self._conn.execute("SELECT * FROM sources WHERE url=?", (url,)).fetchone()
        return _row_to_source(row)

    def get_source(self, source_id: int) -> Source | None:
        row = self._conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return _row_to_source(row) if row else None

    def list_sources(self) -> list[Source]:
        rows = self._conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return [_row_to_source(r) for r in rows]

    def remove_source(self, source_id: int) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM sources WHERE id=?", (source_id,))
        return cur.rowcount > 0

    def update_source(self, source_id: int, **kwargs: Any) -> None:
        allowed = {"name", "interval_sec", "pages", "status", "seed_existing", "min_price",
                   "max_price", "keywords_include", "keywords_exclude", "last_check_at",
                   "next_check_at", "last_error", "consecutive_errors", "found_total",
                   "notified_total"}
        sets, values = [], []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if isinstance(value, list):
                value = ",".join(value)
            if isinstance(value, bool):
                value = int(value)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(source_id)
        with self.tx() as c:
            c.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", values)

    def due_sources(self, now_ts: float | None = None) -> list[Source]:
        now_ts = now_ts if now_ts is not None else time.time()
        rows = self._conn.execute(
            "SELECT * FROM sources WHERE status=? AND next_check_at<=? ORDER BY next_check_at",
            (SourceStatus.ACTIVE.value, now_ts),
        ).fetchall()
        return [_row_to_source(r) for r in rows]

    # ---------- listings ----------
    def upsert_listing(self, listing: Listing, *, mark_seen_only: bool = False) -> str:
        """Возвращает 'new' | 'price_drop' | 'price_up' | 'known'.

        'new' — объявления раньше не было (кандидат на уведомление).
        """
        now = time.time()
        chash = content_hash(listing.title, listing.price, listing.description, listing.image_url)
        row = self._conn.execute("SELECT price, notified_at, content_hash FROM listings WHERE item_id=?",
                                 (listing.item_id,)).fetchone()
        published = listing.published_at.astimezone(MSK).isoformat() if listing.published_at else None
        payload = (
            listing.source_id, listing.url, listing.title, listing.price, listing.price_text,
            listing.currency, listing.old_price, listing.discount_pct, listing.image_url,
            json.dumps(listing.images, ensure_ascii=False), listing.description, published,
            listing.published_raw, listing.location, listing.seller_name, listing.seller_type,
            listing.params, listing.price_source, chash,
            json.dumps(listing.raw, ensure_ascii=False) if listing.raw else None,
        )
        if row is None:
            with self.tx() as c:
                c.execute(
                    """INSERT INTO listings(item_id,source_id,url,title,price,price_text,currency,
                          old_price,discount_pct,image_url,images_json,description,published_at,
                          published_raw,location,seller_name,seller_type,params,price_source,
                          content_hash,raw_json,first_seen_at,last_seen_at,notified_at,seen_count)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (listing.item_id, *payload, now, now, now if mark_seen_only else None),
                )
                if listing.price is not None:
                    c.execute("INSERT INTO price_history(item_id,price,changed_at) VALUES(?,?,?)",
                              (listing.item_id, listing.price, now))
            return "new"

        old_price = row["price"]
        outcome = "known"
        if listing.price is not None and old_price is not None and listing.price != old_price:
            outcome = "price_drop" if listing.price < old_price else "price_up"
        with self.tx() as c:
            c.execute(
                """UPDATE listings SET
                     source_id=COALESCE(?,source_id), url=?, title=?,
                     price=COALESCE(?,price), price_text=?, currency=?,
                     old_price=COALESCE(?,old_price), discount_pct=COALESCE(?,discount_pct),
                     image_url=COALESCE(?,image_url),
                     images_json=CASE WHEN ?!='[]' THEN ? ELSE images_json END,
                     description=COALESCE(?,description),
                     published_at=COALESCE(?,published_at),
                     published_raw=COALESCE(?,published_raw),
                     location=COALESCE(?,location), seller_name=COALESCE(?,seller_name),
                     seller_type=COALESCE(?,seller_type), params=COALESCE(?,params),
                     price_source=?, content_hash=?, raw_json=COALESCE(?,raw_json),
                     last_seen_at=?, seen_count=seen_count+1
                   WHERE item_id=?""",
                (listing.source_id, listing.url, listing.title, listing.price, listing.price_text,
                 listing.currency, listing.old_price, listing.discount_pct, listing.image_url,
                 json.dumps(listing.images, ensure_ascii=False),
                 json.dumps(listing.images, ensure_ascii=False), listing.description, published,
                 listing.published_raw, listing.location, listing.seller_name, listing.seller_type,
                 listing.params, listing.price_source, chash,
                 json.dumps(listing.raw, ensure_ascii=False) if listing.raw else None,
                 now, listing.item_id),
            )
            if outcome in ("price_drop", "price_up"):
                c.execute("INSERT INTO price_history(item_id,price,changed_at) VALUES(?,?,?)",
                          (listing.item_id, listing.price, now))
        return outcome

    def get_listing(self, item_id: str) -> Listing | None:
        row = self._conn.execute("SELECT * FROM listings WHERE item_id=?", (item_id,)).fetchone()
        return _row_to_listing(row) if row else None

    def known_ids(self, item_ids: list[str]) -> set[str]:
        if not item_ids:
            return set()
        found: set[str] = set()
        chunk = 400
        for i in range(0, len(item_ids), chunk):
            part = item_ids[i:i + chunk]
            marks = ",".join("?" * len(part))
            rows = self._conn.execute(
                f"SELECT item_id FROM listings WHERE item_id IN ({marks})", part).fetchall()
            found.update(r["item_id"] for r in rows)
        return found

    def mark_enriched(self, item_id: str) -> None:
        self._conn.execute("UPDATE listings SET enriched_at=? WHERE item_id=?", (time.time(), item_id))

    def mark_notified(self, item_id: str, error: str | None = None) -> None:
        with self.tx() as c:
            if error:
                c.execute("UPDATE listings SET notify_error=? WHERE item_id=?", (error, item_id))
            else:
                c.execute("UPDATE listings SET notified_at=?, notify_error=NULL WHERE item_id=?",
                          (time.time(), item_id))

    def pending_notifications(self, limit: int = 50) -> list[Listing]:
        rows = self._conn.execute(
            "SELECT * FROM listings WHERE notified_at IS NULL ORDER BY first_seen_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_listing(r) for r in rows]

    def bump_source_counters(self, source_id: int, found: int = 0, notified: int = 0) -> None:
        with self.tx() as c:
            c.execute("UPDATE sources SET found_total=found_total+?, notified_total=notified_total+? WHERE id=?",
                      (found, notified, source_id))

    def price_history(self, item_id: str, limit: int = 10) -> list[tuple[float, int | None]]:
        rows = self._conn.execute(
            "SELECT changed_at, price FROM price_history WHERE item_id=? ORDER BY changed_at DESC LIMIT ?",
            (item_id, limit)).fetchall()
        return [(r["changed_at"], r["price"]) for r in rows]

    def stats(self) -> dict[str, Any]:
        one = lambda q: self._conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "sources": one("SELECT COUNT(*) FROM sources"),
            "sources_active": one(f"SELECT COUNT(*) FROM sources WHERE status='{SourceStatus.ACTIVE.value}'"),
            "listings": one("SELECT COUNT(*) FROM listings"),
            "notified": one("SELECT COUNT(*) FROM listings WHERE notified_at IS NOT NULL"),
            "pending": one("SELECT COUNT(*) FROM listings WHERE notified_at IS NULL"),
            "captcha_total": one("SELECT COUNT(*) FROM captcha_events"),
            # 'manual' и 'manual-answer' — оба решены человеком, автоматикой считаем остальное
            "captcha_solved_auto": one("SELECT COUNT(*) FROM captcha_events WHERE solved=1 "
                                       "AND method NOT IN ('manual','manual-answer')"),
            "captcha_solved_manual": one("SELECT COUNT(*) FROM captcha_events WHERE solved=1 "
                                         "AND method IN ('manual','manual-answer')"),
            "last24h_new": one(f"SELECT COUNT(*) FROM listings WHERE first_seen_at>{time.time() - 86400}"),
            "db_size_mb": round(self.path.stat().st_size / 1e6, 2) if self.path.exists() else 0,
        }

    # ---------- captcha ----------
    def log_captcha(self, source_id: int | None, variant: str, url: str, *, method: str = "",
                    solved: bool = False, attempts: int = 0, detail: str = "",
                    screenshot: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO captcha_events(source_id,variant,url,method,solved,attempts,detail,
                                              screenshot,created_at,resolved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, variant, url, method, int(solved), attempts, detail, screenshot,
                 time.time(), time.time() if solved else None),
            )
        return int(cur.lastrowid)

    def resolve_captcha(self, event_id: int, method: str, solved: bool, detail: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE captcha_events SET method=?, solved=?, resolved_at=?, detail=? WHERE id=?",
                      (method, int(solved), time.time(), detail, event_id))

    def last_captcha_at(self, source_id: int | None) -> float | None:
        row = self._conn.execute(
            "SELECT created_at FROM captcha_events WHERE source_id IS ? ORDER BY created_at DESC LIMIT 1",
            (source_id,)).fetchone()
        return row["created_at"] if row else None

    def add_captcha_sample(self, path: str, variant: str, label: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO captcha_samples(path,label,variant,created_at) VALUES(?,?,?,?)",
                      (path, label, variant, time.time()))

    def export_listings(self, limit: int = 10000) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT item_id,url,title,price,price_text,published_at,location,image_url,description,"
            "first_seen_at FROM listings ORDER BY first_seen_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"], url=row["url"], name=row["name"], interval_sec=row["interval_sec"],
        pages=row["pages"], status=row["status"], seed_existing=bool(row["seed_existing"]),
        min_price=row["min_price"], max_price=row["max_price"],
        keywords_include=[s for s in (row["keywords_include"] or "").split(",") if s],
        keywords_exclude=[s for s in (row["keywords_exclude"] or "").split(",") if s],
        last_check_at=row["last_check_at"], next_check_at=row["next_check_at"],
        last_error=row["last_error"], consecutive_errors=row["consecutive_errors"],
        found_total=row["found_total"], notified_total=row["notified_total"],
        created_at=row["created_at"],
    )


def _row_to_listing(row: sqlite3.Row) -> Listing:
    published = None
    if row["published_at"]:
        try:
            published = datetime.fromisoformat(row["published_at"])
        except ValueError:
            published = None
    return Listing(
        item_id=row["item_id"], url=row["url"], title=row["title"], price=row["price"],
        price_text=row["price_text"], currency=row["currency"], old_price=row["old_price"],
        discount_pct=row["discount_pct"], image_url=row["image_url"],
        images=json.loads(row["images_json"] or "[]"), description=row["description"],
        published_at=published, published_raw=row["published_raw"], location=row["location"],
        seller_name=row["seller_name"], seller_type=row["seller_type"], params=row["params"],
        source_id=row["source_id"], price_source=row["price_source"] or "unknown",
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
    )