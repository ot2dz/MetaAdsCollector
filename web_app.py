#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web UI and API for DZ-AdSpy E-commerce Intelligence
Runs: python web_app.py
Access at: http://localhost:5001
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

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_cors import CORS

from meta_ads_collector.collector import MetaAdsCollector
from meta_ads_collector.constants import (
    AD_TYPE_ALL,
    SEARCH_EXACT,
    SEARCH_KEYWORD,
    SEARCH_PAGE,
    STATUS_ACTIVE,
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
from db import db, extract_store_domain
from meta_ads_collector.dedup import DeduplicationTracker
from meta_ads_collector.filters import FilterConfig
from meta_ads_collector.models import Ad
from meta_ads_collector.proxy_pool import ProxyPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("web_app")

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            msg = record.getMessage()
            if any(p in msg for p in ["/api/stream", "/api/status", "/api/db/stats"]):
                return
            self.q.put({
                "type": "log",
                "data": {
                    "text": msg,
                    "logger": record.name,
                    "level": record.levelname,
                    "time": datetime.now().strftime("%H:%M:%S")
                }
            })
        except Exception:
            pass

app = Flask(__name__)
CORS(app)

class CollectionManager:
    """Manages the collection session."""

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
            "duration_seconds": 0.0,
            "start_time": time.time(),
        }

    def stop(self):
        logger.info("Emergency kill-switch activated by user!")
        self.stop_requested.set()
        self.stats["is_running"] = False
        if self.collector and hasattr(self.collector, "client"):
            try:
                self.collector.client.close()
            except Exception:
                pass

    def run_collection(self, config: dict[str, Any]):
        self.reset()
        start_mono = time.monotonic()

        # Check proxies from input or fallback to database
        proxy_val = config.get("proxy", "").strip()
        proxies_list = []
        if proxy_val:
            proxies_list = [p.strip() for p in proxy_val.split("\n") if p.strip() and not p.startswith("#")]
        else:
            proxies_list = db.get_active_proxies()

        proxy_pool = None
        if len(proxies_list) > 1:
            proxy_pool = ProxyPool(proxies_list)
        elif len(proxies_list) == 1:
            proxy_pool = proxies_list[0]

        rate_limit_delay = float(config.get("delay", 2.0))
        timeout = int(config.get("timeout", 30))

        # Setup filters
        filters = None
        has_video = config.get("has_video")
        has_image = config.get("has_image")
        start_date_str = config.get("start_date") or config.get("start_date_min")
        end_date_str = config.get("end_date") or config.get("start_date_max")

        start_dt = None
        end_dt = None
        clean_start_date = None
        clean_end_date = None

        if start_date_str:
            try:
                clean_start_date = str(start_date_str).split("T")[0]
                start_dt = datetime.fromisoformat(clean_start_date)
            except Exception:
                pass
        if end_date_str:
            try:
                clean_end_date = str(end_date_str).split("T")[0]
                end_dt = datetime.fromisoformat(clean_end_date).replace(hour=23, minute=59, second=59)
            except Exception:
                pass

        if any(v is not None for v in [has_video, has_image, start_dt, end_dt]):
            filters = FilterConfig(
                has_video=bool(has_video) if has_video is not None else None,
                has_image=bool(has_image) if has_image is not None else None,
                start_date=start_dt,
                end_date=end_dt,
            )

        try:
            self.collector = MetaAdsCollector(
                proxy=proxy_pool,
                rate_limit_delay=rate_limit_delay,
                timeout=timeout,
            )

            # Events
            def on_started(evt: Event):
                self.event_queue.put({"type": "started", "data": evt.data})

            def on_page(evt: Event):
                self.stats["pages_fetched"] = evt.data.get("page_number", 0)
                if evt.data.get("total_available"):
                    self.stats["total_available"] = evt.data.get("total_available")
                self.event_queue.put({"type": "page_fetched", "data": evt.data})

            def on_ad(evt: Event):
                ad_obj: Ad = evt.data.get("ad")
                if ad_obj:
                    ad_dict = ad_obj.to_dict()
                    creatives = ad_dict.get("creatives") or []
                    primary = creatives[0] if creatives else {}
                    link_url = primary.get("link_url") or ""
                    
                    # Extract store domain
                    store_domain, store_platform = extract_store_domain(link_url)
                    ad_dict["store_domain"] = store_domain
                    ad_dict["store_platform"] = store_platform

                    # STRICT STORE FILTER: Ignore ads that do not possess a real store domain
                    only_stores = config.get("stores_only", True)
                    if only_stores and not store_domain:
                        return

                    self.collected_ads.append(ad_dict)
                    self.stats["ads_collected"] = len(self.collected_ads)
                    
                    # Persist to database
                    try:
                        db.save_ad(ad_dict, query=config.get("query", ""), country="DZ", stores_only=only_stores)
                    except Exception as db_err:
                        logger.warning("Error persisting ad %s: %s", ad_dict.get("id"), db_err)

                    self.event_queue.put({"type": "ad", "data": ad_dict})

            def on_error(evt: Event):
                self.stats["errors"] += 1
                self.event_queue.put({"type": "error", "data": {"context": evt.data.get("context", "Error")}})

            self.collector.event_emitter.on(COLLECTION_STARTED, on_started)
            self.collector.event_emitter.on(PAGE_FETCHED, on_page)
            self.collector.event_emitter.on(AD_COLLECTED, on_ad)
            self.collector.event_emitter.on(ERROR_OCCURRED, on_error)

            # 1. Custom Keyword or Automatic Trick for All Algeria Ads
            raw_query = config.get("query", "").strip()
            if raw_query:
                query = raw_query
                search_type = config.get("search_type", SEARCH_KEYWORD)
                logger.info(f"Targeting custom keyword: {query!r} (Search Type: {search_type})")
            else:
                # Master trick: ' ' exact phrase pulls ALL Algerian Arabic ads from newest to oldest!
                query = " "
                search_type = SEARCH_EXACT
                logger.info("Targeting ALL Algerian Ads (Global Trick: query=' ', exact phrase)")

            country = "DZ"
            ad_type = config.get("ad_type", AD_TYPE_ALL)
            status = config.get("status", STATUS_ACTIVE)
            # Strictly newest to oldest
            sort_by = "SORT_BY_RELEVANCY_MONTHLY_GROUPED"

            # Deduplication mode: only enable when explicitly requested
            is_incremental = bool(config.get("incremental", False))
            dedup_tracker = None
            max_consecutive_seen = None

            if is_incremental:
                db_path = os.path.join(os.path.dirname(__file__), "collection_state.db")
                dedup_tracker = DeduplicationTracker(mode="persistent", db_path=db_path)
                max_consecutive_seen = int(config.get("max_consecutive_seen", 25))

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
                stop_event=self.stop_requested,
                start_date=clean_start_date,
                end_date=clean_end_date,
            )

            for _ in search_generator:
                if self.stop_requested.is_set():
                    logger.info("Collection interrupted by stop signal")
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

            try:
                db.log_collection(
                    query=config.get("query", "ALL_DZ"),
                    country="DZ",
                    total_collected=len(self.collected_ads),
                    duration_seconds=self.stats["duration_seconds"],
                )
            except Exception as log_err:
                logger.warning("Failed to log collection run: %s", log_err)

            self.event_queue.put({
                "type": "finished",
                "data": {
                    "total_collected": len(self.collected_ads),
                    "duration": self.stats["duration_seconds"],
                },
            })

manager = CollectionManager()

queue_log_handler = QueueLogHandler(manager.event_queue)
queue_log_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(queue_log_handler)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/start", methods=["POST"])
def api_start():
    if manager.stats["is_running"]:
        if manager.thread and manager.thread.is_alive():
            return jsonify({"error": "جلسة سحب قيد التشغيل بالفعل."}), 400
        else:
            manager.stats["is_running"] = False

    config = request.get_json(force=True, silent=True) or {}
    logger.info("بدء جولة رصد وسحب جديدة (DZ-AdSpy Core)...")
    manager.thread = threading.Thread(target=manager.run_collection, args=(config,), daemon=True)
    manager.thread.start()
    return jsonify({"status": "started", "message": "Radar collection initiated successfully."})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    manager.stop()
    return jsonify({"status": "stopping", "message": "Stop signal sent."})

# Helper to detect current outbound IP used by the system
_cached_ip_info = {"ip": "127.0.0.1", "is_proxy": False, "checked_at": 0}

def get_outbound_ip_info(current_proxy: Optional[str] = None) -> dict[str, Any]:
    global _cached_ip_info
    now = time.time()
    # Cache for 20 seconds to prevent hammering ipify
    if now - _cached_ip_info["checked_at"] < 20 and not current_proxy:
        return _cached_ip_info

    try:
        from curl_cffi.requests import Session as CffiSession
        s = CffiSession(impersonate="chrome")
        if current_proxy:
            s.proxies = {"http": current_proxy, "https": current_proxy}
        
        resp = s.get("https://api.ipify.org?format=json", timeout=6)
        if resp.status_code == 200:
            ip = resp.json().get("ip")
            _cached_ip_info = {
                "ip": ip,
                "is_proxy": bool(current_proxy),
                "proxy_host": current_proxy.split("@")[-1] if current_proxy else None,
                "checked_at": now
            }
    except Exception as exc:
        logger.debug("IP detection note: %s", exc)

    return _cached_ip_info

@app.route("/api/status", methods=["GET"])
def api_status():
    stats = dict(manager.stats)
    if stats["is_running"] and stats["start_time"]:
        stats["duration_seconds"] = round(time.time() - stats["start_time"], 1)
    
    # Attach current outbound IP and proxy status
    current_proxy = manager.collector.client._current_proxy if (manager.collector and hasattr(manager.collector, "client")) else None
    stats["network"] = get_outbound_ip_info(current_proxy)
    return jsonify(stats)

@app.route("/api/stream")
def api_stream():
    def event_stream():
        while True:
            try:
                item = manager.event_queue.get(timeout=1.0)
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

# ── Stores Directory API ───────────────────────────────────────────────────

@app.route("/api/stores", methods=["GET"])
def api_stores():
    platform = request.args.get("platform", "").strip()
    search = request.args.get("search", "").strip()
    stores = db.get_stores_directory(platform=platform, search=search)
    return jsonify({"stores": stores, "total": len(stores)})

# ── Watchlist API ──────────────────────────────────────────────────────────

@app.route("/api/watchlist", methods=["GET"])
def api_get_watchlist():
    items = db.get_watchlist()
    return jsonify({"watchlist": items})

@app.route("/api/watchlist", methods=["POST"])
def api_add_watchlist():
    data = request.get_json(force=True, silent=True) or {}
    target = data.get("target", "").strip()
    if not target:
        return jsonify({"error": "يرجى إدخال رابط المنافس أو معرّف الصفحة."}), 400
    
    name = data.get("name", "")
    store_domain, platform = extract_store_domain(target)
    if not store_domain and "." in target:
        store_domain = target
    
    db.add_watchlist_item(target=target, name=name or store_domain or target, store_domain=store_domain or "", platform=platform or "")
    return jsonify({"status": "success", "message": "تمت إضافة المنافس للرادار بنجاح."})

@app.route("/api/watchlist/<int:item_id>", methods=["DELETE"])
def api_delete_watchlist(item_id: int):
    db.remove_watchlist_item(item_id)
    return jsonify({"status": "deleted", "id": item_id})

# ── Ads & Database APIs ────────────────────────────────────────────────────

@app.route("/api/db/ads", methods=["GET"])
def api_db_ads():
    limit = int(request.args.get("limit", 60))
    offset = int(request.args.get("offset", 0))
    query = request.args.get("query", "").strip()
    country = request.args.get("country", "").strip()
    stores_only = request.args.get("stores_only", "true").lower() == "true"

    ads = db.get_ads(limit=limit, offset=offset, query=query, country=country, stores_only=stores_only)
    total = db.count_ads(query=query, country=country, stores_only=stores_only)
    return jsonify({"ads": ads, "total": total, "limit": limit, "offset": offset})

@app.route("/api/db/stats", methods=["GET"])
def api_db_stats():
    return jsonify(db.get_stats())

@app.route("/api/db/proxies", methods=["GET"])
def api_db_proxies():
    return jsonify({"proxies": db.get_all_proxies()})

@app.route("/api/db/proxies", methods=["POST"])
def api_db_add_proxies():
    data = request.get_json(force=True, silent=True) or {}
    proxy_text = data.get("proxies", "")
    added = db.add_proxies_bulk(proxy_text)
    return jsonify({"status": "success", "added_count": added})

@app.route("/api/db/proxies/<int:proxy_id>", methods=["DELETE"])
def api_db_delete_proxy(proxy_id: int):
    db.delete_proxy(proxy_id)
    return jsonify({"status": "deleted", "proxy_id": proxy_id})

@app.route("/api/db/proxies/clear", methods=["POST"])
def api_db_clear_proxies():
    count = db.clear_proxies()
    return jsonify({"status": "cleared", "count": count})

@app.route("/api/db/reset", methods=["POST"])
def api_db_reset():
    db.factory_reset()
    return jsonify({"status": "reset", "message": "تمت إعادة تهيئة قاعدة البيانات بالكامل."})

@app.route("/api/export/<fmt>", methods=["GET"])
def api_export(fmt: str):
    ads = manager.collected_ads or db.get_ads(limit=500, stores_only=True)
    fmt = fmt.lower()

    if fmt == "json":
        buf = io.BytesIO(json.dumps(ads, indent=2, ensure_ascii=False).encode("utf-8"))
        return send_file(buf, as_attachment=True, download_name=f"dz_ads_{int(time.time())}.json", mimetype="application/json")
    elif fmt == "csv":
        output = io.StringIO()
        columns = ["id", "page_name", "store_domain", "store_platform", "delivery_start_time", "body", "link_url", "image_url", "video_url", "cta_text"]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for ad in ads:
            creatives = ad.get("creatives", [])
            primary = creatives[0] if creatives else {}
            page = ad.get("page") or {}
            writer.writerow({
                "id": ad.get("id"),
                "page_name": page.get("name", "") if isinstance(page, dict) else ad.get("page_name", ""),
                "store_domain": ad.get("store_domain", ""),
                "store_platform": ad.get("store_platform", ""),
                "delivery_start_time": ad.get("delivery_start_time", ""),
                "body": primary.get("body", "") if isinstance(primary, dict) else ad.get("body", ""),
                "link_url": primary.get("link_url", "") if isinstance(primary, dict) else ad.get("link_url", ""),
                "image_url": primary.get("image_url", "") if isinstance(primary, dict) else ad.get("image_url", ""),
                "video_url": primary.get("video_url", "") if isinstance(primary, dict) else ad.get("video_url", ""),
                "cta_text": primary.get("cta_text", "") if isinstance(primary, dict) else ad.get("cta_text", ""),
            })
        buf = io.BytesIO(output.getvalue().encode("utf-8"))
        return send_file(buf, as_attachment=True, download_name=f"dz_ads_{int(time.time())}.csv", mimetype="text/csv")

    return jsonify({"error": f"Unsupported format: {fmt}"}), 400

if __name__ == "__main__":
    print("=" * 65)
    print(" 🚀 DZ-AdSpy Platform Running on http://127.0.0.1:5001")
    print("=" * 65)
    app.run(host="0.0.0.0", port=5001, debug=False)