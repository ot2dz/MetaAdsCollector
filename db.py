# -*- coding: utf-8 -*-
"""
Database Management for Meta Ads Collector
===========================================
Supports PostgreSQL in production (via DATABASE_URL from Coolify/Docker)
with an automatic SQLite fallback for local development.

Manages:
- Storing and deduplicating collected ads
- Persistent proxy pool management (add, delete, status tracking)
- Collection history & statistics
- Deletion & Factory Reset
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("meta_ads_db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


class DatabaseManager:
    """Unified Database Manager supporting PostgreSQL and SQLite."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or DATABASE_URL
        self.is_postgres = bool(
            self.db_url and (self.db_url.startswith("postgres://") or self.db_url.startswith("postgresql://"))
        )
        if self.is_postgres and self.db_url.startswith("postgres://"):
            # Fix standard SQLAlchemy / Heroku / Coolify url format
            self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)

        self.sqlite_path = os.path.join(os.path.dirname(__file__), "meta_ads_db.sqlite")
        self.init_db()

    def get_connection(self):
        if self.is_postgres:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(self.db_url)
            conn.autocommit = True
            return conn
        else:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            return conn

    def _execute(self, query: str, params: tuple = (), fetch: str = "none") -> Any:
        """Execute a query adapting placeholder syntax (%s for Postgres, ? for SQLite)."""
        conn = self.get_connection()
        try:
            if not self.is_postgres:
                # Convert %s to ? for SQLite
                sqlite_query = query.replace("%s", "?")
                cur = conn.cursor()
                cur.execute(sqlite_query, params)
                if fetch == "one":
                    res = cur.fetchone()
                    return dict(res) if res else None
                elif fetch == "all":
                    res = cur.fetchall()
                    return [dict(r) for r in res]
                conn.commit()
                return cur.lastrowid
            else:
                import psycopg2.extras
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(query, params)
                    if fetch == "one":
                        res = cur.fetchone()
                        return dict(res) if res else None
                    elif fetch == "all":
                        res = cur.fetchall()
                        return [dict(r) for r in res]
                    return None
        finally:
            conn.close()

    def init_db(self):
        """Initialize database tables for ads, proxies, and logs."""
        logger.info(f"Initializing database (Backend: {'PostgreSQL' if self.is_postgres else 'SQLite'})...")

        if self.is_postgres:
            # PostgreSQL schema
            self._execute("""
                CREATE TABLE IF NOT EXISTS ads (
                    id VARCHAR(64) PRIMARY KEY,
                    page_id VARCHAR(64),
                    page_name TEXT,
                    page_profile_pic TEXT,
                    body TEXT,
                    title TEXT,
                    link_url TEXT,
                    image_url TEXT,
                    video_url TEXT,
                    cta_text VARCHAR(100),
                    impressions_text VARCHAR(100),
                    spend_text VARCHAR(100),
                    currency VARCHAR(16),
                    publisher_platforms TEXT,
                    is_active BOOLEAN,
                    delivery_start_time VARCHAR(64),
                    search_query VARCHAR(255),
                    country VARCHAR(10),
                    collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    raw_json JSONB
                );
            """)

            self._execute("""
                CREATE TABLE IF NOT EXISTS proxies (
                    id SERIAL PRIMARY KEY,
                    proxy_url TEXT UNIQUE NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    failures INTEGER DEFAULT 0,
                    last_used TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            self._execute("""
                CREATE TABLE IF NOT EXISTS collection_logs (
                    id SERIAL PRIMARY KEY,
                    query VARCHAR(255),
                    country VARCHAR(10),
                    total_collected INTEGER,
                    duration_seconds REAL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
        else:
            # SQLite schema
            self._execute("""
                CREATE TABLE IF NOT EXISTS ads (
                    id TEXT PRIMARY KEY,
                    page_id TEXT,
                    page_name TEXT,
                    page_profile_pic TEXT,
                    body TEXT,
                    title TEXT,
                    link_url TEXT,
                    image_url TEXT,
                    video_url TEXT,
                    cta_text TEXT,
                    impressions_text TEXT,
                    spend_text TEXT,
                    currency TEXT,
                    publisher_platforms TEXT,
                    is_active INTEGER,
                    delivery_start_time TEXT,
                    search_query TEXT,
                    country TEXT,
                    collected_at TEXT,
                    raw_json TEXT
                );
            """)

            self._execute("""
                CREATE TABLE IF NOT EXISTS proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_url TEXT UNIQUE NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    failures INTEGER DEFAULT 0,
                    last_used TEXT,
                    created_at TEXT
                );
            """)

            self._execute("""
                CREATE TABLE IF NOT EXISTS collection_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    country TEXT,
                    total_collected INTEGER,
                    duration_seconds REAL,
                    created_at TEXT
                );
            """)

    # ── Ads Management ────────────────────────────────────────────────────────

    def save_ad(self, ad: dict[str, Any], query: str = "", country: str = "DZ") -> bool:
        """Insert or update an ad in the database."""
        ad_id = str(ad.get("id") or "")
        if not ad_id:
            return False

        page = ad.get("page") or {}
        creatives = ad.get("creatives") or []
        primary_creative = creatives[0] if creatives else {}
        imp = ad.get("impressions") or {}
        spd = ad.get("spend") or {}

        imp_str = f"{imp.get('lower_bound', '')} - {imp.get('upper_bound', '')}" if imp else ""
        spd_str = f"{spd.get('lower_bound', '')} - {spd.get('upper_bound', '')}" if spd else ""
        platforms = ",".join(ad.get("publisher_platforms") or [])
        now_iso = datetime.now(timezone.utc).isoformat()

        raw_data_str = json.dumps(ad, ensure_ascii=False)

        params = (
            ad_id,
            str(page.get("id") or ""),
            page.get("name") or "",
            page.get("profile_picture_url") or "",
            primary_creative.get("body") or "",
            primary_creative.get("title") or "",
            primary_creative.get("link_url") or "",
            primary_creative.get("image_url") or primary_creative.get("thumbnail_url") or "",
            primary_creative.get("video_url") or primary_creative.get("video_hd_url") or "",
            primary_creative.get("cta_text") or "",
            imp_str,
            spd_str,
            spd.get("currency") or "",
            platforms,
            1 if ad.get("is_active") else 0,
            ad.get("delivery_start_time") or "",
            query,
            country,
            now_iso,
            raw_data_str,
        )

        if self.is_postgres:
            query_sql = """
                INSERT INTO ads (
                    id, page_id, page_name, page_profile_pic, body, title, link_url,
                    image_url, video_url, cta_text, impressions_text, spend_text,
                    currency, publisher_platforms, is_active, delivery_start_time,
                    search_query, country, collected_at, raw_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    is_active = EXCLUDED.is_active,
                    impressions_text = EXCLUDED.impressions_text,
                    spend_text = EXCLUDED.spend_text,
                    collected_at = EXCLUDED.collected_at,
                    raw_json = EXCLUDED.raw_json;
            """
        else:
            query_sql = """
                INSERT INTO ads (
                    id, page_id, page_name, page_profile_pic, body, title, link_url,
                    image_url, video_url, cta_text, impressions_text, spend_text,
                    currency, publisher_platforms, is_active, delivery_start_time,
                    search_query, country, collected_at, raw_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    is_active = excluded.is_active,
                    impressions_text = excluded.impressions_text,
                    spend_text = excluded.spend_text,
                    collected_at = excluded.collected_at,
                    raw_json = excluded.raw_json;
            """

        self._execute(query_sql, params)
        return True

    def is_ad_seen(self, ad_id: str) -> bool:
        """Check if an ad ID already exists in the database."""
        row = self._execute("SELECT 1 FROM ads WHERE id = %s LIMIT 1", (str(ad_id),), fetch="one")
        return bool(row)

    def get_ads(self, limit: int = 50, offset: int = 0, query: str = "", country: str = "") -> list[dict[str, Any]]:
        """Retrieve stored ads with optional search and pagination."""
        sql = "SELECT * FROM ads WHERE 1=1"
        params: list[Any] = []

        if query:
            sql += " AND (body ILIKE %s OR page_name ILIKE %s OR title ILIKE %s)" if self.is_postgres else " AND (body LIKE %s OR page_name LIKE %s OR title LIKE %s)"
            q_like = f"%{query}%"
            params.extend([q_like, q_like, q_like])

        if country:
            sql += " AND country = %s"
            params.append(country.upper())

        sql += " ORDER BY collected_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = self._execute(sql, tuple(params), fetch="all") or []
        for r in rows:
            if isinstance(r.get("raw_json"), str):
                try:
                    r["raw_json"] = json.loads(r["raw_json"])
                except Exception:
                    pass
        return rows

    def count_ads(self, query: str = "", country: str = "") -> int:
        """Count total ads stored in database."""
        sql = "SELECT COUNT(*) as count FROM ads WHERE 1=1"
        params: list[Any] = []

        if query:
            sql += " AND (body ILIKE %s OR page_name ILIKE %s OR title ILIKE %s)" if self.is_postgres else " AND (body LIKE %s OR page_name LIKE %s OR title LIKE %s)"
            q_like = f"%{query}%"
            params.extend([q_like, q_like, q_like])

        if country:
            sql += " AND country = %s"
            params.append(country.upper())

        res = self._execute(sql, tuple(params), fetch="one")
        return res["count"] if res else 0

    def delete_ad(self, ad_id: str) -> bool:
        """Delete a single ad by ID."""
        self._execute("DELETE FROM ads WHERE id = %s", (str(ad_id),))
        return True

    def clear_ads(self) -> int:
        """Delete all ads from database."""
        count = self.count_ads()
        self._execute("DELETE FROM ads")
        return count

    # ── Proxies Management ────────────────────────────────────────────────────

    def add_proxy(self, proxy_url: str) -> bool:
        """Add a proxy to the database."""
        p_clean = proxy_url.strip()
        if not p_clean or p_clean.startswith("#"):
            return False

        now_iso = datetime.now(timezone.utc).isoformat()
        if self.is_postgres:
            sql = """
                INSERT INTO proxies (proxy_url, is_active, failures, created_at)
                VALUES (%s, TRUE, 0, %s)
                ON CONFLICT (proxy_url) DO UPDATE SET is_active = TRUE;
            """
        else:
            sql = """
                INSERT INTO proxies (proxy_url, is_active, failures, created_at)
                VALUES (%s, 1, 0, %s)
                ON CONFLICT (proxy_url) DO UPDATE SET is_active = 1;
            """
        self._execute(sql, (p_clean, now_iso))
        return True

    def add_proxies_bulk(self, proxy_text: str) -> int:
        """Add multiple proxies from multiline string."""
        added = 0
        for line in proxy_text.splitlines():
            if self.add_proxy(line):
                added += 1
        return added

    def get_active_proxies(self) -> list[str]:
        """Return list of active proxy strings for ProxyPool."""
        sql = "SELECT proxy_url FROM proxies WHERE is_active = TRUE" if self.is_postgres else "SELECT proxy_url FROM proxies WHERE is_active = 1"
        rows = self._execute(sql, fetch="all") or []
        return [r["proxy_url"] for r in rows]

    def get_all_proxies(self) -> list[dict[str, Any]]:
        """Return all proxies with their operational stats."""
        sql = "SELECT * FROM proxies ORDER BY id DESC"
        return self._execute(sql, fetch="all") or []

    def update_proxy_status(self, proxy_url: str, success: bool):
        """Update proxy operational metrics."""
        now_iso = datetime.now(timezone.utc).isoformat()
        if success:
            sql = "UPDATE proxies SET failures = 0, last_used = %s WHERE proxy_url = %s"
            self._execute(sql, (now_iso, proxy_url))
        else:
            sql = "UPDATE proxies SET failures = failures + 1, last_used = %s WHERE proxy_url = %s"
            self._execute(sql, (now_iso, proxy_url))
            # Deactivate if failures exceed threshold
            deactivate_sql = "UPDATE proxies SET is_active = FALSE WHERE proxy_url = %s AND failures >= 5" if self.is_postgres else "UPDATE proxies SET is_active = 0 WHERE proxy_url = %s AND failures >= 5"
            self._execute(deactivate_sql, (proxy_url,))

    def delete_proxy(self, proxy_id: int) -> bool:
        """Delete a proxy from database."""
        self._execute("DELETE FROM proxies WHERE id = %s", (proxy_id,))
        return True

    def clear_proxies(self) -> int:
        """Delete all proxies from database."""
        proxies = self.get_all_proxies()
        self._execute("DELETE FROM proxies")
        return len(proxies)

    # ── System Logs & Factory Reset ───────────────────────────────────────────

    def log_collection(self, query: str, country: str, total_collected: int, duration_seconds: float):
        """Record collection run metrics."""
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = "INSERT INTO collection_logs (query, country, total_collected, duration_seconds, created_at) VALUES (%s, %s, %s, %s, %s)"
        self._execute(sql, (query, country, total_collected, duration_seconds, now_iso))

    def get_stats(self) -> dict[str, Any]:
        """Return overall database statistics."""
        ads_count = self.count_ads()
        proxies_total = len(self.get_all_proxies())
        proxies_active = len(self.get_active_proxies())
        last_log = self._execute("SELECT * FROM collection_logs ORDER BY id DESC LIMIT 1", fetch="one")

        return {
            "backend": "PostgreSQL" if self.is_postgres else "SQLite",
            "total_ads": ads_count,
            "total_proxies": proxies_total,
            "active_proxies": proxies_active,
            "last_collection": last_log,
        }

    def factory_reset(self) -> bool:
        """Complete reset: drop and recreate all tables."""
        logger.warning("FACTORY RESET TRIGGERED: Rebuilding database tables...")
        if self.is_postgres:
            self._execute("DROP TABLE IF EXISTS ads CASCADE;")
            self._execute("DROP TABLE IF EXISTS proxies CASCADE;")
            self._execute("DROP TABLE IF EXISTS collection_logs CASCADE;")
        else:
            self._execute("DROP TABLE IF EXISTS ads;")
            self._execute("DROP TABLE IF EXISTS proxies;")
            self._execute("DROP TABLE IF EXISTS collection_logs;")
        self.init_db()
        return True


# Global database instance
db = DatabaseManager()