# -*- coding: utf-8 -*-
"""
Standalone Diagnostic Tool with Pagination & Deep Telemetry
Runs multi-page search for queries like "متجر جودة" or "foorweb.store",
inspecting what Facebook reports on each page, tracking filters, stores, and cursors.

Run via:
  python3 test_newest_search.py "متجر جودة"
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from curl_cffi.requests import Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def extract_store_domain(url: str) -> tuple[str | None, str | None]:
    """Classify URLs to see if it's a real store, WhatsApp/Messenger, or other."""
    if not url or not isinstance(url, str):
        return None, None

    url_clean = url.strip()
    if re.search(r"wa\.me|whatsapp\.com|api\.whatsapp", url_clean, re.IGNORECASE):
        return None, "WhatsApp"
    if re.search(r"m\.me|messenger\.com", url_clean, re.IGNORECASE):
        return None, "Messenger"
    if re.search(r"facebook\.com|fb\.com|instagram\.com|t\.me", url_clean, re.IGNORECASE):
        return None, "Social Link"

    try:
        if not url_clean.startswith(("http://", "https://")):
            url_clean = "https://" + url_clean
        parsed = urlparse(url_clean)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if "." not in netloc or len(netloc) < 4:
            return None, "Invalid URL"

        platform = "Custom Domain"
        if "youcan.shop" in netloc or "youcan.store" in netloc:
            platform = "YouCan Shop"
        elif "myshopify.com" in netloc or "shopify" in url_clean:
            platform = "Shopify"
        elif netloc.endswith(".dz"):
            platform = "Algerian (.dz)"

        return netloc, platform
    except Exception:
        return None, "Error"

def parse_raw_graphql_text(text: str) -> dict:
    clean_text = text.strip()
    if clean_text.startswith("for (;;);"):
        clean_text = clean_text[9:].strip()

    decoder = json.JSONDecoder()
    idx = 0
    chunks = []
    while idx < len(clean_text):
        while idx < len(clean_text) and clean_text[idx] in " \n\r\t":
            idx += 1
        if idx >= len(clean_text):
            break
        try:
            obj, end = decoder.raw_decode(clean_text, idx)
            if isinstance(obj, dict):
                chunks.append(obj)
            idx = end
        except Exception:
            break

    for chunk in chunks:
        chunk_data = chunk.get("data", {}) if isinstance(chunk, dict) else {}
        if isinstance(chunk_data, dict):
            alm = chunk_data.get("ad_library_main") or chunk_data.get("adLibraryMain")
            if isinstance(alm, dict) and ("search_results_connection" in alm or "searchResultsConnection" in alm):
                return chunk

    for chunk in chunks:
        if "search_results_connection" in json.dumps(chunk, ensure_ascii=False):
            return chunk

    return chunks[0] if chunks else (json.loads(clean_text) if clean_text else {})

def run_pagination_telemetry(query: str = "متجر جودة", country: str = "DZ", max_pages: int = 5):
    print("=" * 80)
    print(f"🚀 بدء التشخيص التكتيكي المتقدم للبحث: {query!r}")
    print(f"🇩🇿 الدولة: {country} | فلترة اللغة: عربية (ar) | الترتيب: من الأحدث للأقدم")
    print(f"📑 الحد الأقصى للصفحات المفحوصة في هذا الاختبار: {max_pages} صفحات")
    print("=" * 80)

    session = Session(impersonate="chrome")
    session.cookies.set("wd", "1920x1080", domain=".facebook.com", path="/")
    session.cookies.set("dpr", "1.25", domain=".facebook.com", path="/")

    home_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "ar,en-US;q=0.9,en;q=0.8",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }

    print("\n[1] استخراج التوكنات والكوكيز من فيسبوك...")
    resp = session.get("https://www.facebook.com/", headers=home_headers, timeout=20)
    lsd_match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"\}', resp.text) or re.search(r'name="lsd" value="([^"]+)"', resp.text)
    lsd = lsd_match.group(1) if lsd_match else ""
    if not lsd:
        print("❌ فشل استخراج توكن LSD")
        return
    jazoest = str(2 + sum(ord(c) for c in lsd))
    print(f"   -> تم استخراج LSD: {lsd[:10]}... | jazoest: {jazoest}")

    cursor = None
    page_num = 1
    total_raw_ads = 0
    total_valid_stores = 0
    total_whatsapp = 0
    total_social_or_none = 0
    initial_fb_count = None

    doc_id = "26617181747964058"
    friendly_name = "AdLibraryMobileFocusedStateProviderRefetchQuery"
    search_type = "KEYWORD_EXACT_PHRASE" if query.strip() == "" else "KEYWORD_UNORDERED"

    while page_num <= max_pages:
        print("\n" + "—" * 80)
        print(f"📡 [الصفحة #{page_num}] إرسال طلب استرجاع الدفعة... (Cursor: {cursor[:20] if cursor else 'البداية'})")

        variables = {
            "activeStatus": "ACTIVE",
            "adType": "ALL",
            "audienceTimeframe": "LAST_7_DAYS",
            "bylines": [],
            "collationToken": "e9bacba9-2cda-46a1-bb83-d3b25b035aa9",
            "contentLanguages": ["ar"],
            "countries": [country],
            "country": country,
            "deeplinkAdID": None,
            "excludedIDs": [],
            "fetchPageInfo": False,
            "fetchSharedDisclaimers": False,
            "hasDeeplinkAdID": False,
            "isAboutTab": False,
            "isAudienceTab": False,
            "isLandingPage": False,
            "isTargetedCountry": False,
            "location": None,
            "mediaType": "ALL",
            "multiCountryFilterMode": None,
            "pageIDs": [],
            "potentialReachInput": [],
            "publisherPlatforms": [],
            "queryString": query,
            "regions": [],
            "searchType": search_type,
            "sessionID": "e9bacba9-2cda-46a1-bb83-d3b25b035aa9",
            "sortData": {
                "mode": "SORT_BY_RELEVANCY_MONTHLY_GROUPED",
                "direction": "DESCENDING"
            },
            "source": None,
            "startDate": None,
            "v": "15e849",
            "viewAllPageID": "0"
        }
        if cursor:
            variables["cursor"] = cursor

        payload = {
            "av": "0",
            "__aaid": "0",
            "__user": "0",
            "__a": "1",
            "__req": str(page_num),
            "__hs": "20727.HYP:comet_plat_default_pkg.2.1...0",
            "dpr": "2",
            "__ccg": "EXCELLENT",
            "__rev": "1048989295",
            "__s": "33yjzt:e5s4ss:f8l83x",
            "__hsi": str(int(time.time() * 1000)),
            "__dyn": "7xe6E5q9zo5ObwKBAg5S1Dxu13wqovzEdF8aUco2qwJwCwfW7oqx609vCyU4a0qa2O1Vwooa8462mcw5Mx62G3i1ywOwv89k2C1Fwc61Axi2a7o2ezXwrUcUjwGzE2VKUbo5G0zK5o4q0HU1IEGdw46wbLwrU6C2-0VE6O1FwlU3NG2O1Tw4-w3C8do",
            "__csr": "hr5nmT6Ijb5kyqi4ndPl6zsQAzEB6teKA8SF3sIJiEIIB998AARdSGL9nA8JsikxfyWGVl9F4eF5RhrIyWjprGjCDhVudyTV4-Fp4by9FXy8JA_oyqATl7KA_Hh94bKviqal6KihavguwmEOfyXwJxu7Fob8S1owgUvw8a1dwqp81go12Euw7rx62K1Rw5ew9e4obo4y0iW0wE3QwVwHw7jwhu0su02ga0JU0svw3ko3lw4Fwae5Ukwzw28Hw0aqBk00JR801gcS0b8w9G02XS0n-05g4U",
            "__hsdp": "ge97kYJK7-zog2ARNv5m01N4w0c6606Fo0byU0bLU09i8",
            "__hblp": "045w3u81XE1Ho1cE1no2Tw0gSU13o2Hw0xcw1Mm02TW0e6xa0159w21E0OW0m-0aoyo3Hw12K0fgw38U6y4U0WyE2mw8K5o28w2I809i80J-0WU5y",
            "__sjsp": "ge94Gfbrx_ES40FdsnNlw",
            "__comet_req": "94",
            "lsd": lsd,
            "jazoest": jazoest,
            "__spin_r": "1048989295",
            "__spin_b": "trunk",
            "__spin_t": str(int(time.time())),
            "__jssesw": "1",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name,
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": doc_id,
        }

        gql_headers = {
            "accept": "*/*",
            "accept-language": "ar,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&content_languages[0]=ar&country=DZ&is_targeted_country=false&media_type=all&q=%22%20%22&search_type=keyword_exact_phrase&sort_data[mode]=relevancy_monthly_grouped&sort_data[direction]=desc",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": home_headers["user-agent"],
            "x-asbd-id": "359341",
            "x-fb-friendly-name": friendly_name,
            "x-fb-lsd": lsd,
        }

        t0 = time.time()
        res = session.post("https://www.facebook.com/api/graphql/", data=payload, headers=gql_headers, timeout=25)
        elapsed = round(time.time() - t0, 2)

        data = parse_raw_graphql_text(res.text)
        conn = data.get("data", {}).get("ad_library_main", {}).get("search_results_connection", {})
        fb_reported_total = conn.get("count", 0)
        edges = conn.get("edges", [])
        page_info = conn.get("page_info", {})

        has_next = page_info.get("has_next_page", False)
        next_cursor = page_info.get("end_cursor")

        if initial_fb_count is None:
            initial_fb_count = fb_reported_total

        print(f"⏱️ زمن الاستجابة: {elapsed} ثانية | كود السيرفر: {res.status_code}")
        print(f"📊 إحصائية فيسبوك اللحظية لهذه الصفحة: [ Count المعلن: {fb_reported_total:,} إعلان ] (الأولي كان: {initial_fb_count:,})")
        print(f"📦 الإعلانات الخام المسترجعة في هذه الصفحة: {len(edges)} إعلان | هل توجد صفحة تالية؟: {'نعم ✅' if has_next else 'لا (نهاية النتائج) 🛑'}")

        if not edges:
            print("⚠️ لم يعد فيسبوك أي إعلانات في هذه الصفحة. انتهاء السحب.")
            break

        total_raw_ads += len(edges)
        page_stores = 0
        page_whatsapp = 0
        page_other = 0

        print("\n🔍 تفكيك إعلانات هذه الصفحة لمعرفة الوجهات (Destinations):")
        for i, edge in enumerate(edges, 1):
            collated = edge.get("node", {}).get("collated_results", [{}])[0]
            ad_id = collated.get("ad_archive_id")
            page_name = collated.get("page_name") or collated.get("snapshot", {}).get("page_name")
            start_ts = collated.get("start_date")
            date_str = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M") if start_ts else "غير محدد"
            link = collated.get("link_url") or collated.get("snapshot", {}).get("link_url") or ""

            domain, platform = extract_store_domain(link)

            if domain:
                status_tag = f"✅ متجر مؤكد [{platform}]: {domain}"
                page_stores += 1
                total_valid_stores += 1
            elif platform in ["WhatsApp", "Messenger"]:
                status_tag = f"❌ مستبعد ({platform})"
                page_whatsapp += 1
                total_whatsapp += 1
            else:
                status_tag = f"⚠️ بدون متجر ({platform or 'لا يوجد رابط'})"
                page_other += 1
                total_social_or_none += 1

            if i <= 8 or domain:
                print(f"   [{i}] {status_tag} | {page_name[:22]} | 📅 {date_str} | ID: {ad_id}")

        print(f"\n📈 حصاد الصفحة #{page_num}: {page_stores} متجر حقيقي | {page_whatsapp} واتساب ومسنجر مستبعد | {page_other} روابط أخرى/بدون رابط")
        print(f"🏆 المجموع التراكمي حتى الآن: {total_valid_stores} متجر حقيقي مقبول (من أصل {total_raw_ads} إعلان خام)")

        if not has_next or not next_cursor:
            print("\n🏁 وصلنا لنهاية جميع الصفحات المتاحة في فيسبوك لهذا الاستعلام.")
            break

        cursor = next_cursor
        page_num += 1
        time.sleep(1.8)

    print("\n" + "=" * 80)
    print("📊 التقرير النهائي الشامل:")
    print(f"   • إجمالي فيسبوك الأولي المعلن  : {initial_fb_count:,} إعلان")
    print(f"   • إجمالي الإعلانات الخام المستلمة: {total_raw_ads} إعلان")
    print(f"   • ✅ إعلانات المتاجر الحقيقية المقبولة: {total_valid_stores} متجر")
    print(f"   • ❌ إعلانات الواتساب والمسنجر المستبعدة: {total_whatsapp} إعلان")
    print(f"   • ⚠️ إعلانات بدون روابط أو روابط تواصل: {total_social_or_none} إعلان")
    print("=" * 80)

if __name__ == "__main__":
    target_query = sys.argv[1] if len(sys.argv) > 1 else "متجر جودة"
    run_pagination_telemetry(query=target_query, max_pages=4)