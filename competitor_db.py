# -*- coding: utf-8 -*-
"""
Competitor & Brand Dossier Database
====================================
Stores competitor brands, their marketing campaigns, and all tracked
Facebook posts with engagement metrics over time.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("competitor_db")

DB_PATH = os.path.join(os.path.dirname(__file__), "competitors.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize SQLite database tables for brands, campaigns, and tracked posts."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS brands (
                page_id TEXT PRIMARY KEY,
                page_name TEXT NOT NULL,
                page_profile_pic TEXT,
                page_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id TEXT NOT NULL,
                ad_id TEXT,
                hook_snippet TEXT NOT NULL,
                full_ad_text TEXT,
                total_views INTEGER DEFAULT 0,
                total_reactions INTEGER DEFAULT 0,
                total_comments INTEGER DEFAULT 0,
                earliest_date TEXT,
                latest_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (page_id) REFERENCES brands(page_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                post_url TEXT NOT NULL UNIQUE,
                post_title TEXT,
                published_date TEXT,
                views INTEGER DEFAULT 0,
                reactions INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                is_winner INTEGER DEFAULT 0,
                scraped_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_page ON campaigns(page_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_campaign ON campaign_posts(campaign_id)")
        conn.commit()


# Initialize database on module load
init_db()


def save_brand_campaign_dossier(
    page_id: str,
    page_name: str,
    page_profile_pic: Optional[str],
    page_url: Optional[str],
    ad_id: Optional[str],
    hook_snippet: str,
    full_ad_text: str,
    posts_data: list[dict[str, Any]],
) -> dict[str, Any]:
    """Save or update a brand, its campaign, and all discovered posts with metrics."""
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        # 1. Upsert Brand
        conn.execute("""
            INSERT INTO brands (page_id, page_name, page_profile_pic, page_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                page_name = excluded.page_name,
                page_profile_pic = COALESCE(excluded.page_profile_pic, brands.page_profile_pic),
                page_url = COALESCE(excluded.page_url, brands.page_url),
                updated_at = excluded.updated_at
        """, (page_id, page_name, page_profile_pic, page_url, now_iso, now_iso))

        # Calculate totals
        total_views = sum(int(p.get("views") or 0) for p in posts_data)
        total_reactions = sum(int(p.get("reactions") or 0) for p in posts_data)
        total_comments = sum(int(p.get("comments") or 0) for p in posts_data)

        # 2. Check if this campaign already exists for this page
        cur = conn.execute(
            "SELECT id FROM campaigns WHERE page_id = ? AND hook_snippet = ?",
            (page_id, hook_snippet),
        )
        existing = cur.fetchone()

        if existing:
            campaign_id = existing["id"]
            conn.execute("""
                UPDATE campaigns SET
                    total_views = ?, total_reactions = ?, total_comments = ?,
                    updated_at = ?
                WHERE id = ?
            """, (total_views, total_reactions, total_comments, now_iso, campaign_id))
        else:
            cur = conn.execute("""
                INSERT INTO campaigns (
                    page_id, ad_id, hook_snippet, full_ad_text,
                    total_views, total_reactions, total_comments,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                page_id, ad_id, hook_snippet, full_ad_text,
                total_views, total_reactions, total_comments,
                now_iso, now_iso
            ))
            campaign_id = cur.lastrowid

        # 3. Find the Winner post (highest views or reactions)
        max_score = -1
        winner_idx = 0
        for i, p in enumerate(posts_data):
            score = (int(p.get("views") or 0) * 10) + int(p.get("reactions") or 0)
            if score > max_score:
                max_score = score
                winner_idx = i

        # 4. Upsert Posts
        for i, p in enumerate(posts_data):
            is_win = 1 if (i == winner_idx and max_score > 0) else 0
            conn.execute("""
                INSERT INTO campaign_posts (
                    campaign_id, post_url, post_title, published_date,
                    views, reactions, comments, shares, is_winner, scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_url) DO UPDATE SET
                    post_title = excluded.post_title,
                    published_date = COALESCE(excluded.published_date, campaign_posts.published_date),
                    views = excluded.views,
                    reactions = excluded.reactions,
                    comments = excluded.comments,
                    shares = excluded.shares,
                    is_winner = excluded.is_winner,
                    scraped_at = excluded.scraped_at
            """, (
                campaign_id,
                p.get("post_url", ""),
                p.get("post_title", ""),
                p.get("date", ""),
                int(p.get("views") or 0),
                int(p.get("reactions") or 0),
                int(p.get("comments") or 0),
                int(p.get("shares") or 0),
                is_win,
                now_iso,
            ))

        conn.commit()

    return get_brand_dossier(page_id)


def get_all_brands_summary() -> list[dict[str, Any]]:
    """Return a summary list of all tracked competitor brands with aggregate statistics."""
    with get_connection() as conn:
        cur = conn.execute("""
            SELECT
                b.page_id,
                b.page_name,
                b.page_profile_pic,
                b.page_url,
                b.updated_at,
                COUNT(DISTINCT c.id) as campaign_count,
                COALESCE(SUM(c.total_views), 0) as total_brand_views,
                COALESCE(SUM(c.total_reactions), 0) as total_brand_reactions
            FROM brands b
            LEFT JOIN campaigns c ON b.page_id = c.page_id
            GROUP BY b.page_id
            ORDER BY b.updated_at DESC
        """)
        return [dict(row) for row in cur.fetchall()]


def get_brand_dossier(page_id: str) -> dict[str, Any]:
    """Return complete detailed dossier for a competitor brand including all campaigns and posts."""
    with get_connection() as conn:
        # Brand info
        cur_brand = conn.execute("SELECT * FROM brands WHERE page_id = ?", (page_id,))
        brand_row = cur_brand.fetchone()
        if not brand_row:
            return {}

        brand_dict = dict(brand_row)

        # Campaigns
        cur_camps = conn.execute(
            "SELECT * FROM campaigns WHERE page_id = ? ORDER BY id DESC",
            (page_id,),
        )
        campaigns = []
        for camp_row in cur_camps.fetchall():
            camp_dict = dict(camp_row)
            # Posts for this campaign
            cur_posts = conn.execute(
                "SELECT * FROM campaign_posts WHERE campaign_id = ? ORDER BY views DESC, reactions DESC",
                (camp_dict["id"],),
            )
            camp_dict["posts"] = [dict(p) for p in cur_posts.fetchall()]
            campaigns.append(camp_dict)

        brand_dict["campaigns"] = campaigns
        return brand_dict


def delete_brand_dossier(page_id: str) -> bool:
    """Delete a brand and all associated campaigns and posts from the archive."""
    with get_connection() as conn:
        conn.execute("DELETE FROM brands WHERE page_id = ?", (page_id,))
        conn.commit()
    return True