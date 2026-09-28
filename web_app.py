#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web UI for Meta Ads Collector
==============================
Provides an interactive, modern browser-based dashboard to control
MetaAdsCollector, stream collected ads in real-time, view live statistics,
and export datasets in JSON, CSV, or JSONL.

Run:
    python web_app.py
Then open:
    http://localhost:5001
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_cors import CORS

from meta_ads_collector.collector import MetaAdsCollector
from meta_ads_collector.constants import (
    AD_TYPE_ALL,
    AD_TYPE_CREDIT,
    AD_TYPE_EMPLOYMENT,
    AD_TYPE_HOUSING,
    AD_TYPE_POLITICAL,
    SEARCH_EXACT,
    SEARCH_KEYWORD,
    SEARCH_PAGE,
    SORT_IMPRESSIONS,
    SORT_RELEVANCY,
    STATUS_ACTIVE,
    STATUS_ALL,
    STATUS_INACTIVE,
)
from meta_ads_collector.events import (
    AD_COLLECTED,
    COLLECTION_FINISHED,
    COLLECTION_STARTED,
    ERROR_OCCURRED,
    PAGE_FETCHED,
    RATE_LIMITED,
    SESSION_REFRESHED,
    Event,
)
from meta_ads_collector.dedup import DeduplicationTracker
from meta_ads_collector.filters import FilterConfig
from meta_ads_collector.models import Ad
from meta_ads_collector.proxy_pool import ProxyPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("web_app")

app = Flask(__name__)
CORS(app)


class CollectionManager:
    """Manages the lifecycle of an active collection session in a background thread."""

    def __init__(self):
        self.collector: Optional[MetaAdsCollector] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_requested = threading.Event()
        self.event_queue: queue.Queue = queue.Queue()
        self.collected_ads: list[dict[str, Any]] = []
        self.stats: dict[str, Any] = {
            "is_running": False,
            "ads_collected": 0,
            "requests_made": 0,
            "pages_fetched": 0,
            "errors": 0,
            "duplicates_skipped": 0,
            "early_exit": False,
            "duration_seconds": 0.0,
            "start_time": None,
        }

    def reset(self):
        self.stop_requested.clear()
        self.collected_ads.clear()
        self.stats = {
            "is_running": True,
            "ads_collected": 0,
            "requests_made": 0,
            "pages_fetched": 0,
            "errors": 0,
            "duplicates_skipped": 0,
            "early_exit": False,
            "duration_seconds": 0.0,
            "start_time": time.time(),
        }

    def stop(self):
        if self.stats["is_running"]:
            logger.info("Stop requested by user")
            self.stop_requested.set()
            if self.collector:
                try:
                    self.collector.close()
                except Exception:
                    pass

    def run_collection(self, config: dict[str, Any]):
        self.reset()
        start_mono = time.monotonic()

        proxy_val = config.get("proxy")
        proxy_pool = None
        if proxy_val:
            proxies_list = [p.strip() for p in proxy_val.split("\n") if p.strip() and not p.startswith("#")]
            if len(proxies_list) > 1:
                proxy_pool = ProxyPool(proxies_list)
            elif len(proxies_list) == 1:
                proxy_pool = proxies_list[0]

        rate_limit_delay = float(config.get("delay", 2.0))
        timeout = int(config.get("timeout", 30))

        # Setup filters
        filters = None
        min_imp = config.get("min_impressions")
        max_imp = config.get("max_impressions")
        has_video = config.get("has_video")
        has_image = config.get("has_image")

        if any(v is not None for v in [min_imp, max_imp, has_video, has_image]):
            filters = FilterConfig(
                min_impressions=int(min_imp) if min_imp else None,
                max_impressions=int(max_imp) if max_imp else None,
                has_video=bool(has_video) if has_video is not None else None,
                has_image=bool(has_image) if has_image is not None else None,
            )

        try:
            self.collector = MetaAdsCollector(
                proxy=proxy_pool,
                rate_limit_delay=rate_limit_delay,
                timeout=timeout,
            )

            # Wire events
            def on_started(evt: Event):
                self.event_queue.put({"type": "started", "data": evt.data})

            def on_page(evt: Event):
                self.stats["pages_fetched"] = evt.data.get("page_number", 0)
                self.event_queue.put({"type": "page_fetched", "data": evt.data})

            def on_ad(evt: Event):
                ad_obj: Ad = evt.data.get("ad")
                if ad_obj:
                    ad_dict = ad_obj.to_dict()
                    self.collected_ads.append(ad_dict)
                    self.stats["ads_collected"] = len(self.collected_ads)
                    self.event_queue.put({"type": "ad", "data": ad_dict})

            def on_rate_limited(evt: Event):
                self.event_queue.put({"type": "rate_limited", "data": evt.data})

            def on_session_refreshed(evt: Event):
                self.event_queue.put({"type": "session_refreshed", "data": evt.data})

            def on_error(evt: Event):
                self.stats["errors"] += 1
                self.event_queue.put({"type": "error", "data": {"context": evt.data.get("context", "Error")}})

            self.collector.event_emitter.on(COLLECTION_STARTED, on_started)
            self.collector.event_emitter.on(PAGE_FETCHED, on_page)
            self.collector.event_emitter.on(AD_COLLECTED, on_ad)
            self.collector.event_emitter.on(RATE_LIMITED, on_rate_limited)
            self.collector.event_emitter.on(SESSION_REFRESHED, on_session_refreshed)
            self.collector.event_emitter.on(ERROR_OCCURRED, on_error)

            query = config.get("query", "").strip()
            country = config.get("country", "DZ").strip().upper()
            ad_type = config.get("ad_type", AD_TYPE_ALL)
            status = config.get("status", STATUS_ACTIVE)
            search_type = config.get("search_type", SEARCH_KEYWORD)
            
            # Smart Incremental Mode:
            is_incremental = bool(config.get("incremental", False))
            dedup_tracker = None
            max_consecutive_seen = None

            if is_incremental:
                # In incremental mode, sort by relevancy/newest, enable persistent database and early-exit
                sort_by = SORT_RELEVANCY
                db_path = os.path.join(os.path.dirname(__file__), "collection_state.db")
                dedup_tracker = DeduplicationTracker(mode="persistent", db_path=db_path)
                max_consecutive_seen = int(config.get("max_consecutive_seen", 15))
                logger.info("Incremental delta mode enabled with early-exit at %d duplicates", max_consecutive_seen)
            else:
                sort_by = SORT_IMPRESSIONS if config.get("sort_by") == "impressions" else SORT_RELEVANCY

            max_results = int(config.get("max_results")) if config.get("max_results") else None
            page_size = int(config.get("page_size", 10))
            page_ids = [p.strip() for p in config.get("page_ids", "").split(",") if p.strip()] or None

            search_generator = self.collector.search(
                query=query,
                country=country,
                ad_type=ad_type,
                status=status,
                search_type=search_type,
                page_ids=page_ids,
                sort_by=sort_by,
                max_results=max_results,
                page_size=page_size,
                filter_config=filters,
                dedup_tracker=dedup_tracker,
                max_consecutive_seen=max_consecutive_seen,
            )

            for _ in search_generator:
                if self.stop_requested.is_set():
                    logger.info("Collection loop interrupted by stop signal")
                    break

        except Exception as exc:
            logger.error("Exception during collection: %s", exc, exc_info=True)
            self.stats["errors"] += 1
            self.event_queue.put({"type": "error", "data": {"context": str(exc)}})
        finally:
            self.stats["duration_seconds"] = round(time.monotonic() - start_mono, 2)
            self.stats["is_running"] = False
            if self.collector:
                try:
                    self.collector.close()
                except Exception:
                    pass
            self.event_queue.put({
                "type": "finished",
                "data": {
                    "total_collected": len(self.collected_ads),
                    "duration": self.stats["duration_seconds"],
                    "early_exit": self.stats.get("early_exit", False),
                },
            })


manager = CollectionManager()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    if manager.stats["is_running"]:
        return jsonify({"error": "A collection session is already running."}), 400

    config = request.get_json(force=True, silent=True) or {}
    manager.thread = threading.Thread(target=manager.run_collection, args=(config,), daemon=True)
    manager.thread.start()
    return jsonify({"status": "started", "message": "Collection initiated successfully."})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    manager.stop()
    return jsonify({"status": "stopping", "message": "Stop signal sent."})


@app.route("/api/dedup_stats", methods=["GET"])
def api_dedup_stats():
    """Return number of tracked ads in persistent deduplication database."""
    db_path = os.path.join(os.path.dirname(__file__), "collection_state.db")
    count = 0
    last_run = None
    if os.path.exists(db_path):
        tracker = DeduplicationTracker(mode="persistent", db_path=db_path)
        count = tracker.count()
        last = tracker.get_last_collection_time()
        last_run = last.isoformat() if last else None
        tracker.close()
    return jsonify({"total_seen_ads": count, "last_collection_time": last_run})


@app.route("/api/clear_dedup", methods=["POST"])
def api_clear_dedup():
    """Clear persistent deduplication history."""
    db_path = os.path.join(os.path.dirname(__file__), "collection_state.db")
    if os.path.exists(db_path):
        tracker = DeduplicationTracker(mode="persistent", db_path=db_path)
        tracker.clear()
        tracker.close()
    return jsonify({"status": "cleared", "message": "تم تصفية سجل الإعلانات السابقة بنجاح."})


@app.route("/api/status", methods=["GET"])
def api_status():
    stats = dict(manager.stats)
    if stats["is_running"] and stats["start_time"]:
        stats["duration_seconds"] = round(time.time() - stats["start_time"], 1)
    return jsonify(stats)


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events (SSE) endpoint to push real-time collection updates to browser."""

    def event_stream():
        while True:
            try:
                # Wait for next event
                item = manager.event_queue.get(timeout=1.0)
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                # Send heartbeat keep-alive
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


def extract_search_snippet(text: str) -> str:
    """Extract a clean, meaningful hook sentence from ad text, avoiding URLs and CTAs."""
    if not text:
        return ""

    import re

    # 1. إزالة جميع الروابط بالكامل حتى لا تتسبب النقاط (dots) في قص الكلمات
    clean = re.sub(r"https?://\S+|www\.\S+", " ", text)

    # 2. إزالة أرقام الهواتف والإيميلات والهاشتاجات
    clean = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", clean)
    clean = re.sub(r"\+?\d[\d\s\-\(\)]{7,}\d", " ", clean)
    clean = re.sub(r"#\S+", " ", clean)

    # 3. أخذ السطر الأول (في إعلانات فيسبوك السطر الأول دائماً هو العنوان الجاذب)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    first_block = lines[0] if lines else clean

    # 4. إيقاف الجملة عند كلمات وروابط الطلب الشائعة
    cta_patterns = [
        r"(?:للطلب|لطلب|الطلب|اضغط|للشراء|سارع|تواصل|عبر الرابط|من هنا|رابط المتجر|متجرنا|علقي|كومنتي).*",
    ]
    hook_text = first_block
    for cta_pat in cta_patterns:
        match = re.search(cta_pat, hook_text, flags=re.IGNORECASE)
        if match and match.start() >= 25:
            hook_text = hook_text[:match.start()]
            break

    # 5. تنظيف المسافات والفواصل الزائدة
    words = [w for w in re.split(r"\s+", hook_text) if w and not re.match(r"^[:;,.!؟?()\[\]{}]+$", w)]

    # إذا كانت الكلمات قليلة جداً بعد القص، نأخذ الكلمات الأولى من النص الكامل النظيف
    if len(words) < 5:
        words = [w for w in re.split(r"\s+", clean) if w and not re.match(r"^[:;,.!؟?()\[\]{}]+$", w)]

    # 6. اختيار من 8 إلى 12 كلمة (بين 45 و 75 حرفاً) لتكوين جملة بحث دقيقة ومتكاملة
    selected_words = []
    total_len = 0
    for w in words:
        if total_len + len(w) + 1 > 75 and len(selected_words) >= 6:
            break
        selected_words.append(w)
        total_len += len(w) + 1
        if total_len >= 55 and len(selected_words) >= 8:
            break

    snippet = " ".join(selected_words).strip('"\',.:؛،!?؟ ')
    return snippet


SERPER_API_KEY = "f0d48f72f32309b925b35ee8d536ced36b345a2c"


def scrape_facebook_engagement(post_url: str) -> dict[str, Any]:
    """Lightweight pure-python extraction of reactions, comments, shares from public Facebook posts."""
    import re
    from curl_cffi.requests import Session as CffiSession

    metrics = {
        "reactions": None,
        "comments": None,
        "shares": None,
        "views": None,
    }

    try:
        session = CffiSession(impersonate="chrome")
        session.cookies.set("wd", "1920x1080", domain=".facebook.com")
        session.cookies.set("dpr", "1", domain=".facebook.com")

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

        resp = session.get(post_url, headers=headers, timeout=8, allow_redirects=True)
        if resp.status_code != 200:
            return metrics

        html = resp.text

        # 1. استخراج التفاعلات الحقيقية (Reactions)
        react_match = re.search(r'"reaction_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
        if not react_match:
            react_match = re.search(r'"i18n_reaction_count"\s*:\s*"([^"]+)"', html)
        if react_match:
            metrics["reactions"] = react_match.group(1)

        # 2. استخراج التعليقات (Comments)
        comment_match = re.search(r'"comment_count"\s*:\s*\{\s*"total_count"\s*:\s*(\d+)', html)
        if not comment_match:
            comment_match = re.search(r'"total_comment_count"\s*:\s*(\d+)', html)
        if not comment_match:
            comment_match = re.search(r'"i18n_comment_count"\s*:\s*"([^"]+)"', html)
        if comment_match:
            metrics["comments"] = comment_match.group(1)

        # 3. استخراج المشاركات (Shares)
        share_match = re.search(r'"share_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
        if not share_match:
            share_match = re.search(r'"i18n_share_count"\s*:\s*"([^"]+)"', html)
        if share_match:
            metrics["shares"] = share_match.group(1)

        logger.info("Facebook engagement metrics: %s", metrics)

    except Exception as exc:
        logger.warning("Facebook request note: %s", exc)

    return metrics


def search_facebook_post_engagement(ad_text: str, page_name: str = "") -> dict[str, Any]:
    """Search Google via Serper.dev API to accurately find the original Facebook post."""
    import os
    import re
    import urllib.parse
    import requests

    api_key = os.environ.get("SERPER_API_KEY", SERPER_API_KEY)

    snippet = extract_search_snippet(ad_text)
    if not snippet or len(snippet) < 10:
        return {
            "found": False,
            "error": "نص الإعلان قصير جداً لاستخراج جملة بحث مميزة.",
            "snippet": snippet,
        }

    logger.info("=" * 65)
    logger.info("🚀 [Serper.dev API] فحص تفاعل الإعلان للجملة: %r", snippet)

    search_query_exact = f'"{snippet}" site:facebook.com'
    google_web_url = f"https://www.google.com/search?q={urllib.parse.quote(search_query_exact)}"

    endpoint = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    found_url = None
    post_title = None
    reactions = None
    date_str = None

    def parse_serper_results(items: list[dict[str, Any]]) -> bool:
        nonlocal found_url, post_title, reactions, date_str
        for item in items:
            link = item.get("link", "")
            title = item.get("title", "")
            item_snippet = item.get("snippet", "")

            # التأكد من أنه رابط منشور أو فيديو في فيسبوك
            if "facebook.com" in link:
                clean_link = link.split("&")[0].split("?")[0].rstrip("/.,;")
                if not any(bad in clean_link for bad in ["/login", "/help", "/policies", "/ads", "/public", "/recover", "sharer", "/pages/category"]):
                    found_url = link
                    post_title = title

                    # استخراج التفاعلات من مقتطف النتيجة أو الـ attributes
                    combined_text = f"{title} {item_snippet} {json.dumps(item.get('attributes', {}), ensure_ascii=False)}"
                    m_react = re.search(r'([\d,.]+[KMBkmb]?\+?\s*(?:reactions?|تفاعل|likes?|إعجاب|views?|مشاهدة))', combined_text, re.IGNORECASE)
                    if m_react:
                        reactions = m_react.group(1).strip()

                    # استخراج التاريخ
                    date_val = item.get("date")
                    if date_val:
                        date_str = date_val
                    else:
                        m_date = re.search(r'(\d+\s*(?:days?|hours?|months?|weeks?|أيام|ساعات|أشهر|يوم|شهر)\s*ago|منذ\s*\d+\s*(?:يوم|أيام|ساعة|ساعات))', combined_text, re.IGNORECASE)
                        if m_date:
                            date_str = m_date.group(1).strip()

                    logger.info("🎯 [Serper.dev] تم العثور على البوست الأصلي: %s", found_url)
                    logger.info("📊 التفاعل: %s | التاريخ: %s", reactions, date_str)
                    return True
        return False

    try:
        # المحاولة 1: بحث دقيق بالجملة المطابقة تماماً
        payload = {
            "q": search_query_exact,
            "gl": "dz",
            "hl": "ar",
            "num": 5,
        }
        res = requests.post(endpoint, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            data = res.json()
            organic = data.get("organic", [])
            parse_serper_results(organic)
        else:
            logger.warning("Serper API returned status: %d - %s", res.status_code, res.text)

        # المحاولة 2: إذا لم يجد مع التنصيص، نبحث بدون تنصيص لمرونة تامة
        if not found_url:
            logger.info("محاولة البحث بدون علامات تنصيص لمرونة أكبر...")
            query_broad = f"{snippet} site:facebook.com"
            payload_broad = {
                "q": query_broad,
                "gl": "dz",
                "hl": "ar",
                "num": 5,
            }
            res_broad = requests.post(endpoint, headers=headers, json=payload_broad, timeout=10)
            if res_broad.status_code == 200:
                data_broad = res_broad.json()
                parse_serper_results(data_broad.get("organic", []))

    except Exception as exc:
        logger.error("خطأ أثناء الاتصال بـ Serper.dev: %s", exc, exc_info=True)
        return {
            "found": False,
            "google_url": google_web_url,
            "snippet": snippet,
            "error": f"خطأ في الاتصال: {exc}",
        }

    logger.info("=" * 65)

    if found_url:
        # فحص عميق للبوست الأصلي على فيسبوك لجلب المشاهدات والتعليقات واللايكات
        fb_metrics = scrape_facebook_engagement(found_url)

        return {
            "found": True,
            "post_url": found_url,
            "post_title": post_title or "",
            "reactions": fb_metrics.get("reactions") or reactions or "متوفر في البوست",
            "comments": fb_metrics.get("comments"),
            "shares": fb_metrics.get("shares"),
            "views": fb_metrics.get("views"),
            "date": date_str or "",
            "snippet": snippet,
            "google_url": google_web_url,
        }

    return {
        "found": False,
        "google_url": google_web_url,
        "snippet": snippet,
        "message": "لم يتم العثور على منشور مطابق في جوجل (غالباً إعلان Dark Post مخفي).",
    }
    
@app.route("/api/inspect_engagement", methods=["POST"])
def api_inspect_engagement():
    data = request.get_json(force=True, silent=True) or {}
    ad_text = data.get("text", "")
    page_name = data.get("page_name", "")

    result = search_facebook_post_engagement(ad_text=ad_text, page_name=page_name)
    return jsonify(result)


# Lightweight API mode - Brand dossier removed


@app.route("/api/search_pages", methods=["GET"])
def api_search_pages():
    query = request.args.get("query", "").strip()
    country = request.args.get("country", "DZ").strip().upper()
    if not query:
        return jsonify({"pages": []})

    collector = MetaAdsCollector(timeout=15)
    try:
        pages = collector.search_pages(query=query, country=country)
        return jsonify({"pages": [p.to_dict() for p in pages]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        collector.close()


@app.route("/api/export/<fmt>", methods=["GET"])
def api_export(fmt: str):
    ads = manager.collected_ads
    fmt = fmt.lower()

    if fmt == "json":
        buf = io.BytesIO(json.dumps(ads, indent=2, ensure_ascii=False).encode("utf-8"))
        filename = f"meta_ads_{int(time.time())}.json"
        return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/json")

    elif fmt == "jsonl":
        lines = [json.dumps(ad, ensure_ascii=False) + "\n" for ad in ads]
        buf = io.BytesIO("".join(lines).encode("utf-8"))
        filename = f"meta_ads_{int(time.time())}.jsonl"
        return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/x-ndjson")

    elif fmt == "csv":
        output = io.StringIO()
        columns = [
            "id", "page_name", "page_id", "is_active", "delivery_start_time",
            "body", "title", "image_url", "video_url", "link_url", "cta_text",
            "impressions_lower", "impressions_upper", "spend_lower", "spend_upper",
            "currency", "publisher_platforms", "collected_at"
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()

        for ad in ads:
            creatives = ad.get("creatives", [])
            primary = creatives[0] if creatives else {}
            page = ad.get("page") or {}
            imp = ad.get("impressions") or {}
            spd = ad.get("spend") or {}

            writer.writerow({
                "id": ad.get("id"),
                "page_name": page.get("name", ""),
                "page_id": page.get("id", ""),
                "is_active": ad.get("is_active"),
                "delivery_start_time": ad.get("delivery_start_time", ""),
                "body": primary.get("body", ""),
                "title": primary.get("title", ""),
                "image_url": primary.get("image_url", ""),
                "video_url": primary.get("video_url") or primary.get("video_hd_url", ""),
                "link_url": primary.get("link_url", ""),
                "cta_text": primary.get("cta_text", ""),
                "impressions_lower": imp.get("lower_bound", ""),
                "impressions_upper": imp.get("upper_bound", ""),
                "spend_lower": spd.get("lower_bound", ""),
                "spend_upper": spd.get("upper_bound", ""),
                "currency": spd.get("currency", ""),
                "publisher_platforms": ",".join(ad.get("publisher_platforms", [])),
                "collected_at": ad.get("collected_at", ""),
            })

        buf = io.BytesIO(output.getvalue().encode("utf-8"))
        filename = f"meta_ads_{int(time.time())}.csv"
        return send_file(buf, as_attachment=True, download_name=filename, mimetype="text/csv")

    return jsonify({"error": f"Unsupported format: {fmt}"}), 400


if __name__ == "__main__":
    print("=" * 65)
    print("  🚀 Meta Ads Collector Web Dashboard is running!")
    print("  🌐 Open in your browser: http://127.0.0.1:5001")
    print("=" * 65)
    app.run(host="0.0.0.0", port=5001, debug=False)