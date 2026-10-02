# -*- coding: utf-8 -*-
"""
Standalone Diagnostic Tool for Meta Ads Search with Date Filtering
Tests Facebook GraphQL format for date range filtering.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from curl_cffi.requests import Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

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

def to_timestamp(date_str: str, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to unix timestamp."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0

def run_diagnosis(query: str = " ", start_date_min: str = "", start_date_max: str = "", country: str = "DZ"):
    print("=" * 75)
    print(f"🚀 فحص استجابة فيسبوك للتواريخ لكلمة: '{query}'")
    print(f"🇩🇿 الدولة: {country} | لغة عربية: ar")
    print(f"📅 التاريخ المستهدف: من [{start_date_min or 'غير محدد'}] إلى [{start_date_max or 'غير محدد'}]")
    print("=" * 75)

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

    resp = session.get("https://www.facebook.com/", headers=home_headers, timeout=20)
    html = resp.text

    lsd_match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"\}', html) or re.search(r'name="lsd" value="([^"]+)"', html)
    lsd = lsd_match.group(1) if lsd_match else ""
    if not lsd:
        print("❌ فشل استخراج رمز LSD!")
        return

    jazoest = str(2 + sum(ord(c) for c in lsd))

    # Format Date Variables: Test Facebook's Exact Structure
    # Facebook expects startDate as an object with min and max timestamps or dates
    ts_min = to_timestamp(start_date_min) if start_date_min else None
    ts_max = to_timestamp(start_date_max, end_of_day=True) if start_date_max else None

    # Test Format A: Object with string dates
    # In Facebook Ad Library URL: start_date[min]=YYYY-MM-DD&start_date[max]=YYYY-MM-DD
    # In GraphQL: startDate: {"min": "YYYY-MM-DD", "max": "YYYY-MM-DD"} OR {"min": timestamp, "max": timestamp}
    start_date_var = None
    if start_date_min or start_date_max:
        start_date_var = {}
        if start_date_min:
            start_date_var["min"] = start_date_min
        if start_date_max:
            start_date_var["max"] = start_date_max

    doc_id = "26617181747964058"
    friendly_name = "AdLibraryMobileFocusedStateProviderRefetchQuery"
    search_type = "KEYWORD_EXACT_PHRASE" if query.strip() == "" else "KEYWORD_UNORDERED"

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
        "startDate": start_date_var,
        "v": "15e849",
        "viewAllPageID": "0"
    }

    payload = {
        "av": "0",
        "__aaid": "0",
        "__user": "0",
        "__a": "1",
        "__req": "1",
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

    # Match browser URL parameters exactly
    referer = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&content_languages[0]=ar&country={country}&is_targeted_country=false&media_type=all&q=%22%20%22&search_type=keyword_exact_phrase&sort_data[mode]=relevancy_monthly_grouped&sort_data[direction]=desc"
    if start_date_min:
        referer += f"&start_date[min]={start_date_min}"
    if start_date_max:
        referer += f"&start_date[max]={start_date_max}"

    gql_headers = {
        "accept": "*/*",
        "accept-language": "ar,en-US;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.facebook.com",
        "referer": referer,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": home_headers["user-agent"],
        "x-asbd-id": "359341",
        "x-fb-friendly-name": friendly_name,
        "x-fb-lsd": lsd,
    }

    print("\n[2] إرسال الاستعلام إلى فيسبوك...")
    gql_resp = session.post("https://www.facebook.com/api/graphql/", data=payload, headers=gql_headers, timeout=25)
    print(f"   -> كود الاستجابة: {gql_resp.status_code}")

    parsed_json = parse_raw_graphql_text(gql_resp.text)
    conn = parsed_json.get("data", {}).get("ad_library_main", {}).get("search_results_connection", {})
    total_count = conn.get("count", 0)
    edges = conn.get("edges", [])

    print(f"\n📊 النتائج الإجمالية بفيسبوك: {total_count:,} إعلان")
    print(f"📦 الإعلانات المستلمة في الدفعة: {len(edges)} إعلان")
    print("-" * 75)

    matching_count = 0
    min_date_obj = datetime.strptime(start_date_min, "%Y-%m-%d").date() if start_date_min else None
    max_date_obj = datetime.strptime(start_date_max, "%Y-%m-%d").date() if start_date_max else None

    for i, edge in enumerate(edges, 1):
        node = edge.get("node", {})
        collated = node.get("collated_results", [{}])[0]
        ad_id = collated.get("ad_archive_id")
        page_name = collated.get("page_name") or collated.get("snapshot", {}).get("page_name")
        start_ts = collated.get("start_date")
        
        ad_date = datetime.fromtimestamp(start_ts).date() if start_ts else None
        date_str = ad_date.strftime("%Y-%m-%d") if ad_date else "غير محدد"

        # Check date filter matching
        is_match = True
        if min_date_obj and ad_date and ad_date < min_date_obj:
            is_match = False
        if max_date_obj and ad_date and ad_date > max_date_obj:
            is_match = False

        status_icon = "✅ مطابق للتاريخ" if is_match else "❌ خارج نطاق التاريخ"
        if is_match:
            matching_count += 1

        print(f"[{i}] {status_icon} | التاريخ: {date_str} | الصفحة: {page_name[:25]} | المعرف: {ad_id}")

    print("=" * 75)
    print(f"🎯 ملخص الفحص: {matching_count} من أصل {len(edges)} إعلانات طابقت التاريخ المطلوب بدقة.")
    print("=" * 75)

if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else " "
    d_min = sys.argv[2] if len(sys.argv) > 2 else ""
    d_max = sys.argv[3] if len(sys.argv) > 3 else ""
    run_diagnosis(query=q, start_date_min=d_min, start_date_max=d_max)