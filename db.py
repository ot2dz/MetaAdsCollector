# -*- coding: utf-8 -*-
"""
Database Management for Meta Ads Collector & E-commerce Intelligence (DZ-AdSpy)
Supports PostgreSQL in production with automatic SQLite fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("meta_ads_db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

def extract_store_domain(url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract clean store domain and detect e-commerce platform.
    Filters out non-store URLs like WhatsApp, Messenger, Facebook, etc.
    """
    if not url or not isinstance(url, str):
        return None, None

    url_clean = url.strip()
    # Exclude social/chat URLs completely
    ignored_patterns = [
        r"wa\.me", r"whatsapp\.com", r"m\.me", r"messenger\.com",
        r"facebook\.com", r"fb\.com", r"fb\.watch", r"instagram\.com",
        r"t\.me", r"telegram\.me", r"tiktok\.com", r"youtube\.com",
        r"bit\.ly", r"linktr\.ee"
    ]
    for pat in ignored_patterns:
        if re.search(pat, url_clean, re.IGNORECASE):
            return None, None

    try:
        if not url_clean.startswith(("http://", "https://")):
            url_clean = "https://" + url_clean
        parsed = urlparse(url_clean)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        
        # Must have at least one dot and valid domain
        if "." not in netloc or len(netloc) < 4:
            return None, None

        # Platform detection
        platform = "Custom Domain"
        if "youcan.shop" in netloc or "youcan.store" in netloc:
            platform = "YouCan Shop"
        elif "myshopify.com" in netloc or "shopify" in url_clean:
            platform = "Shopify"
        elif "woocommerce" in url_clean:
            platform = "WooCommerce"
        elif netloc.endswith(".dz"):
            platform = "Algerian (.dz)"

        return netloc, platform
    except Exception:
        return None, None


class DatabaseManager:
    """Unified Database Manager supporting PostgreSQL and SQLite."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or DATABASE_URL
        self.is_postgres = bool(
            self.db_url and (self.db_url.startswith("postgres://") or self.db_url.startswith("postgresql://"))
        )
        if self.is_postgres and self.db_url.startswith("postgres://"):
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
        conn = self.get_connection()
        try:
            if not self.is_postgres:
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
        """Initialize database tables for ads, proxies, logs, and competitor watchlist."""
        logger.info(f"Initializing database (Backend: {'PostgreSQL' if self.is_postgres else 'SQLite'})...")

        if self.is_postgres:
            self._execute("""
                CREATE TABLE IF NOT EXISTS ads (
                    id VARCHAR(64) PRIMARY KEY,
                    page_id VARCHAR(64),
                    page_name TEXT,
                    page_profile_pic TEXT,
                    body TEXT,
                    title TEXT,
                    link_url TEXT,
                    store_domain TEXT,
                    store_platform VARCHAR(50),
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
                CREATE TABLE IF NOT EXISTS competitor_watchlist (
                    id SERIAL PRIMARY KEY,
                    target_identifier TEXT UNIQUE NOT NULL,
                    name TEXT,
                    store_domain TEXT,
                    platform VARCHAR(50),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    last_inspected TIMESTAMP WITH TIME ZONE,
                    status VARCHAR(20) DEFAULT 'ACTIVE',
                    notes TEXT
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
            self._execute("""
                CREATE TABLE IF NOT EXISTS ads (
                    id TEXT PRIMARY KEY,
                    page_id TEXT,
                    page_name TEXT,
                    page_profile_pic TEXT,
                    body TEXT,
                    title TEXT,
                    link_url TEXT,
                    store_domain TEXT,
                    store_platform TEXT,
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
                CREATE TABLE IF NOT EXISTS competitor_watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_identifier TEXT UNIQUE NOT NULL,
                    name TEXT,
                    store_domain TEXT,
                    platform TEXT,
                    created_at TEXT,
                    last_inspected TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    notes TEXT
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

        # ── Auto-Migration: Ensure store_domain & store_platform exist ────────
        try:
            if not self.is_postgres:
                cols = [r["name"] for r in (self._execute("PRAGMA table_info(ads)", fetch="all") or [])]
                if "store_domain" not in cols:
                    logger.info("Migrating SQLite schema: Adding store_domain column...")
                    self._execute("ALTER TABLE ads ADD COLUMN store_domain TEXT DEFAULT '';")
                if "store_platform" not in cols:
                    logger.info("Migrating SQLite schema: Adding store_platform column...")
                    self._execute("ALTER TABLE ads ADD COLUMN store_platform TEXT DEFAULT 'Custom Domain';")
            else:
                self._execute("""
                    DO $$ 
                    BEGIN 
                        BEGIN
                            ALTER TABLE ads ADD COLUMN store_domain TEXT DEFAULT '';
                        EXCEPTION
                            WHEN duplicate_column THEN NULL;
                        END;
                        BEGIN
                            ALTER TABLE ads ADD COLUMN store_platform VARCHAR(50) DEFAULT 'Custom Domain';
                        EXCEPTION
                            WHEN duplicate_column THEN NULL;
                        END;
                    END $$;
                """)
        except Exception as mig_err:
            logger.warning("Schema migration notice: %s", mig_err)

    # ── Ads Management ────────────────────────────────────────────────────────

    def save_ad(self, ad: dict[str, Any], query: str = "", country: str = "DZ", stores_only: bool = True) -> bool:
        """Insert or update an ad in the database with strict store domain filtering."""
        ad_id = str(ad.get("id") or "")
        if not ad_id:
            return False

        page = ad.get("page") or {}
        creatives = ad.get("creatives") or []
        primary_creative = creatives[0] if creatives else {}
        link_url = primary_creative.get("link_url") or ""

        # Domain and store platform extraction
        store_domain, store_platform = extract_store_domain(link_url)

        # If user strictly requires e-commerce stores only, filter out ads without valid store domains
        if stores_only and not store_domain:
            return False

        imp = ad.get("impressions") or {}
        spd = ad.get("spend") or {}
        imp_str = f"{imp.get('lower_bound', '')} - {imp.get('upper_bound', '')}" if imp else ""
        spd_str = f"{spd.get('lower_bound', '')} - {spd.get('upper_bound', '')}" if spd else ""
        platforms = ",".join(ad.get("publisher_platforms") or [])
        now_iso = datetime.now(timezone.utc).isoformat()
        raw_data_str = json.dumps(ad, ensure_ascii=False)
        is_active_val = bool(ad.get("is_active")) if self.is_postgres else (1 if ad.get("is_active") else 0)

        params = (
            ad_id,
            str(page.get("id") or ""),
            page.get("name") or "",
            page.get("profile_picture_url") or "",
            primary_creative.get("body") or "",
            primary_creative.get("title") or "",
            link_url,
            store_domain or "",
            store_platform or "Custom Domain",
            primary_creative.get("image_url") or primary_creative.get("thumbnail_url") or "",
            primary_creative.get("video_url") or primary_creative.get("video_hd_url") or "",
            primary_creative.get("cta_text") or "",
            imp_str,
            spd_str,
            spd.get("currency") or "",
            platforms,
            is_active_val,
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
                    store_domain, store_platform, image_url, video_url, cta_text,
                    impressions_text, spend_text, currency, publisher_platforms,
                    is_active, delivery_start_time, search_query, country, collected_at, raw_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    is_active = EXCLUDED.is_active,
                    store_domain = EXCLUDED.store_domain,
                    store_platform = EXCLUDED.store_platform,
                    impressions_text = EXCLUDED.impressions_text,
                    spend_text = EXCLUDED.spend_text,
                    collected_at = EXCLUDED.collected_at,
                    raw_json = EXCLUDED.raw_json;
            """
        else:
            query_sql = """
                INSERT INTO ads (
                    id, page_id, page_name, page_profile_pic, body, title, link_url,
                    store_domain, store_platform, image_url, video_url, cta_text,
                    impressions_text, spend_text, currency, publisher_platforms,
                    is_active, delivery_start_time, search_query, country, collected_at, raw_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    is_active = excluded.is_active,
                    store_domain = excluded.store_domain,
                    store_platform = excluded.store_platform,
                    impressions_text = excluded.impressions_text,
                    spend_text = excluded.spend_text,
                    collected_at = excluded.collected_at,
                    raw_json = excluded.raw_json;
            """

        self._execute(query_sql, params)
        return True

    def get_ads(self, limit: int = 60, offset: int = 0, query: str = "", country: str = "", stores_only: bool = True) -> list[dict[str, Any]]:
        """Retrieve stored ads with optional search and pagination."""
        sql = "SELECT * FROM ads WHERE 1=1"
        params: list[Any] = []

        if stores_only:
            sql += " AND store_domain != '' AND store_domain IS NOT NULL"

        if query:
            sql += " AND (body ILIKE %s OR page_name ILIKE %s OR title ILIKE %s OR store_domain ILIKE %s)" if self.is_postgres else " AND (body LIKE %s OR page_name LIKE %s OR title LIKE %s OR store_domain LIKE %s)"
            q_like = f"%{query}%"
            params.extend([q_like, q_like, q_like, q_like])

        if country:
            sql += " AND country = %s"
            params.append(country.upper())

        sql += " ORDER BY collected_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = self._execute(sql, tuple(params), fetch="all") or []
        for r in rows:
            if isinstance(r.get("collected_at"), datetime):
                r["collected_at"] = r["collected_at"].isoformat()
            if isinstance(r.get("raw_json"), str):
                try:
                    r["raw_json"] = json.loads(r["raw_json"])
                except Exception:
                    pass
        return rows

    def count_ads(self, query: str = "", country: str = "", stores_only: bool = True) -> int:
        sql = "SELECT COUNT(*) as count FROM ads WHERE 1=1"
        params: list[Any] = []

        if stores_only:
            try:
                if not self.is_postgres:
                    cols = [r["name"] for r in (self._execute("PRAGMA table_info(ads)", fetch="all") or [])]
                    if "store_domain" in cols:
                        sql += " AND store_domain != '' AND store_domain IS NOT NULL"
                else:
                    sql += " AND store_domain != '' AND store_domain IS NOT NULL"
            except Exception:
                pass

        if query:
            sql += " AND (body ILIKE %s OR page_name ILIKE %s OR title ILIKE %s OR store_domain ILIKE %s)" if self.is_postgres else " AND (body LIKE %s OR page_name LIKE %s OR title LIKE %s OR store_domain LIKE %s)"
            q_like = f"%{query}%"
            params.extend([q_like, q_like, q_like, q_like])

        if country:
            sql += " AND country = %s"
            params.append(country.upper())

        res = self._execute(sql, tuple(params), fetch="one")
        return res["count"] if res else 0

    def get_stores_directory(self, platform: str = "", search: str = "") -> list[dict[str, Any]]:
        """Group ads by store domain to build an Algerian stores directory with telemetry."""
        # Ensure column exists before query
        try:
            if not self.is_postgres:
                cols = [r["name"] for r in (self._execute("PRAGMA table_info(ads)", fetch="all") or [])]
                if "store_domain" not in cols:
                    return []
        except Exception:
            return []

        sql = """
            SELECT 
                store_domain,
                MAX(store_platform) as platform,
                MAX(page_name) as page_name,
                MAX(page_profile_pic) as profile_pic,
                MAX(link_url) as sample_url,
                COUNT(id) as total_ads,
                MIN(delivery_start_time) as oldest_ad_date,
                MAX(delivery_start_time) as newest_ad_date
            FROM ads
            WHERE store_domain != '' AND store_domain IS NOT NULL
        """
        params: list[Any] = []

        if platform and platform.lower() != "all":
            sql += " AND store_platform ILIKE %s" if self.is_postgres else " AND store_platform LIKE %s"
            params.append(f"%{platform}%")

        if search:
            sql += " AND (store_domain ILIKE %s OR page_name ILIKE %s)" if self.is_postgres else " AND (store_domain LIKE %s OR page_name LIKE %s)"
            q_like = f"%{search}%"
            params.extend([q_like, q_like])

        sql += " GROUP BY store_domain ORDER BY total_ads DESC LIMIT 150"
        return self._execute(sql, tuple(params), fetch="all") or []

    # ── Watchlist Management ──────────────────────────────────────────────────

    def add_watchlist_item(self, target: str, name: str = "", store_domain: str = "", platform: str = "") -> bool:
        now_iso = datetime.now(timezone.utc).isoformat()
        if self.is_postgres:
            sql = """
                INSERT INTO competitor_watchlist (target_identifier, name, store_domain, platform, created_at, status)
                VALUES (%s, %s, %s, %s, %s, 'ACTIVE')
                ON CONFLICT (target_identifier) DO UPDATE SET status = 'ACTIVE';
            """
        else:
            sql = """
                INSERT INTO competitor_watchlist (target_identifier, name, store_domain, platform, created_at, status)
                VALUES (%s, %s, %s, %s, %s, 'ACTIVE')
                ON CONFLICT (target_identifier) DO UPDATE SET status = 'ACTIVE';
            """
        self._execute(sql, (target.strip(), name.strip(), store_domain.strip(), platform.strip(), now_iso))
        return True

    def get_watchlist(self) -> list[dict[str, Any]]:
        sql = "SELECT * FROM competitor_watchlist ORDER BY id DESC"
        rows = self._execute(sql, fetch="all") or []
        for r in rows:
            if isinstance(r.get("created_at"), datetime):
                r["created_at"] = r["created_at"].isoformat()
        return rows

    def remove_watchlist_item(self, item_id: int) -> bool:
        self._execute("DELETE FROM competitor_watchlist WHERE id = %s", (item_id,))
        return True

    # ── Proxies & Logs ────────────────────────────────────────────────────────

    def add_proxy(self, proxy_url: str) -> bool:
        p_clean = proxy_url.strip()
        if not p_clean or p_clean.startswith("#"):
            return False
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = "INSERT INTO proxies (proxy_url, is_active, failures, created_at) VALUES (%s, TRUE, 0, %s) ON CONFLICT (proxy_url) DO UPDATE SET is_active = TRUE;" if self.is_postgres else "INSERT INTO proxies (proxy_url, is_active, failures, created_at) VALUES (%s, 1, 0, %s) ON CONFLICT (proxy_url) DO UPDATE SET is_active = 1;"
        self._execute(sql, (p_clean, now_iso))
        return True

    def add_proxies_bulk(self, proxy_text: str) -> int:
        added = 0
        for line in proxy_text.splitlines():
            if self.add_proxy(line):
                added += 1
        return added

    def get_active_proxies(self) -> list[str]:
        sql = "SELECT proxy_url FROM proxies WHERE is_active = TRUE" if self.is_postgres else "SELECT proxy_url FROM proxies WHERE is_active = 1"
        rows = self._execute(sql, fetch="all") or []
        return [r["proxy_url"] for r in rows]

    def get_all_proxies(self) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM proxies ORDER BY id DESC", fetch="all") or []
        for r in rows:
            if isinstance(r.get("created_at"), datetime):
                r["created_at"] = r["created_at"].isoformat()
            if isinstance(r.get("last_used"), datetime):
                r["last_used"] = r["last_used"].isoformat()
        return rows

    def delete_proxy(self, proxy_id: int) -> bool:
        self._execute("DELETE FROM proxies WHERE id = %s", (proxy_id,))
        return True

    def clear_proxies(self) -> int:
        proxies = self.get_all_proxies()
        self._execute("DELETE FROM proxies")
        return len(proxies)

    def log_collection(self, query: str, country: str, total_collected: int, duration_seconds: float):
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = "INSERT INTO collection_logs (query, country, total_collected, duration_seconds, created_at) VALUES (%s, %s, %s, %s, %s)"
        self._execute(sql, (query, country, total_collected, duration_seconds, now_iso))

    def get_stats(self) -> dict[str, Any]:
        ads_count = self.count_ads(stores_only=True)
        all_ads_count = self.count_ads(stores_only=False)
        stores_count = len(self.get_stores_directory())
        watchlist_count = len(self.get_watchlist())
        proxies_total = len(self.get_all_proxies())

        last_log = self._execute("SELECT * FROM collection_logs ORDER BY id DESC LIMIT 1", fetch="one")
        if last_log and isinstance(last_log.get("created_at"), datetime):
            last_log["created_at"] = last_log["created_at"].isoformat()

        return {
            "backend": "PostgreSQL" if self.is_postgres else "SQLite",
            "total_store_ads": int(ads_count),
            "total_all_ads": int(all_ads_count),
            "total_stores": int(stores_count),
            "watchlist_count": int(watchlist_count),
            "total_proxies": proxies_total,
            "last_collection": last_log,
        }

    def clear_ads(self) -> int:
        count = self.count_ads(stores_only=False)
        self._execute("DELETE FROM ads")
        return count

    def factory_reset(self) -> bool:
        if self.is_postgres:
            self._execute("DROP TABLE IF EXISTS ads CASCADE;")
            self._execute("DROP TABLE IF EXISTS competitor_watchlist CASCADE;")
            self._execute("DROP TABLE IF EXISTS proxies CASCADE;")
            self._execute("DROP TABLE IF EXISTS collection_logs CASCADE;")
        else:
            self._execute("DROP TABLE IF EXISTS ads;")
            self._execute("DROP TABLE IF EXISTS competitor_watchlist;")
            self._execute("DROP TABLE IF EXISTS proxies;")
            self._execute("DROP TABLE IF EXISTS collection_logs;")
        self.init_db()
        return True


# Global database instance
db = DatabaseManager()