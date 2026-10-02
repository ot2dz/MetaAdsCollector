# -*- coding: utf-8 -*-
"""
Meta Ads Library HTTP Client

Handles session management, token extraction, and GraphQL requests to the
Facebook Ad Library.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import string
import time
from typing import Any, Optional, Union
from urllib.parse import quote

from curl_cffi.requests import Session as CffiSession
from curl_cffi.requests.exceptions import RequestException as CffiRequestException

from .constants import (
    CHROME_FULL_VERSION,
    CHROME_VERSION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_TIMEOUT,
    DOC_ID_SEARCH,
    DOC_ID_SEARCH_LEGACY,
    DOC_ID_TYPEAHEAD,
    FALLBACK_REV,
    MAX_SESSION_AGE,
    MODERN_CSR,
    MODERN_DYN,
    MODERN_HBLP,
    MODERN_HSDP,
    MODERN_REV,
    MODERN_SJSP,
    USER_AGENT,
)
from .exceptions import (
    AuthenticationError,
    MetaAdsError,
    ProxyError,
    SessionExpiredError,
)
from .fingerprint import BrowserFingerprint, generate_fingerprint
from .proxy_pool import ProxyPool

logger = logging.getLogger(__name__)

class MetaAdsClient:
    """
    HTTP client for Meta Ad Library.

    Manages session state, extracts required tokens from the initial page load,
    and handles GraphQL API requests.
    """

    BASE_URL = "https://www.facebook.com"
    HOME_URL = "https://www.facebook.com/"
    AD_LIBRARY_URL = "https://www.facebook.com/ads/library/"
    GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

    DEFAULT_HEADERS = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": "ar,en-US;q=0.9,en;q=0.8",
        "cache-control": "max-age=0",
        "dpr": "1.25",
        "sec-ch-prefers-color-scheme": "light",
        "sec-ch-ua": (
            f'"Google Chrome";v="{CHROME_VERSION}", '
            f'"Chromium";v="{CHROME_VERSION}", "Not_A Brand";v="24"'
        ),
        "sec-ch-ua-full-version-list": (
            f'"Google Chrome";v="{CHROME_FULL_VERSION}", '
            f'"Chromium";v="{CHROME_FULL_VERSION}", '
            '"Not_A Brand";v="24.0.0.0"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"15.0.0"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": USER_AGENT,
        "viewport-width": "1920",
    }

    GRAPHQL_HEADERS = {
        "accept": "*/*",
        "accept-language": "ar,en-US;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.facebook.com",
        "sec-ch-prefers-color-scheme": "light",
        "sec-ch-ua": f'"Google Chrome";v="{CHROME_VERSION}", "Chromium";v="{CHROME_VERSION}", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"15.0.0"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": USER_AGENT,
        "x-asbd-id": "359341",
    }

    def __init__(
        self,
        proxy: Optional[Union[str, list[str], ProxyPool]] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        max_refresh_attempts: int = 3,
        cookies: Optional[Union[dict, str]] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.max_refresh_attempts = max_refresh_attempts

        self._fingerprint: BrowserFingerprint = generate_fingerprint()
        self.session: Any = CffiSession(impersonate="chrome")
        self._tokens: dict[str, str] = {}
        self._doc_ids: dict[str, str] = {}
        self._initialized = False
        self._request_counter = 0
        self._init_time: Optional[float] = None
        self._consecutive_errors = 0
        self._consecutive_refresh_failures = 0
        self._max_session_age = MAX_SESSION_AGE

        self._provided_cookies: dict[str, str] = self._parse_cookies(cookies)

        self._proxy_pool: Optional[ProxyPool] = None
        self._proxy_string: Optional[str] = None
        self._current_proxy: Optional[str] = None

        if isinstance(proxy, ProxyPool):
            self._proxy_pool = proxy
        elif isinstance(proxy, list):
            self._proxy_pool = ProxyPool(proxy)
        elif isinstance(proxy, str):
            self._proxy_string = proxy
            self._setup_proxy(proxy)

        self.session.headers.update(self._fingerprint.get_default_headers())
        self._apply_provided_cookies()

    @staticmethod
    def _parse_cookies(cookies: Optional[Union[dict, str]]) -> dict[str, str]:
        if not cookies:
            return {}
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items()}
        parsed: dict[str, str] = {}
        for part in str(cookies).split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            parsed[name.strip()] = value.strip()
        return parsed

    def _apply_provided_cookies(self) -> None:
        for name, value in self._provided_cookies.items():
            self.session.cookies.set(name, value, domain=".facebook.com", path="/")

    def _setup_proxy(self, proxy: Optional[str]) -> None:
        if not proxy:
            return

        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, username, password = parts
            proxy_url = f"http://{username}:{password}@{host}:{port}"
        elif len(parts) == 2:
            host, port = parts
            proxy_url = f"http://{host}:{port}"
        else:
            raise ProxyError(f"Invalid proxy format: {proxy!r}. Expected host:port or host:port:user:pass")

        self.session.proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }

    def _extract_tokens(self, html: str) -> dict[str, str]:
        tokens = {}

        lsd_patterns = [
            r'"LSD",\[\],\{"token":"([^"]+)"\}',
            r'\["LSD",\[\],\{"token":"([^"]+)"',
            r'"lsd":"([^"]+)"',
            r'name="lsd" value="([^"]+)"',
        ]
        for pattern in lsd_patterns:
            match = re.search(pattern, html)
            if match:
                tokens["lsd"] = match.group(1)
                break

        rev_patterns = [
            r'"__spin_r":(\d+)',
            r'"server_revision":(\d+)',
            r'"revision":(\d+)',
            r'{"__spin_r":(\d+)',
        ]
        for pattern in rev_patterns:
            match = re.search(pattern, html)
            if match:
                tokens["__rev"] = match.group(1)
                tokens["__spin_r"] = match.group(1)
                break

        spin_t_match = re.search(r'"__spin_t":(\d+)', html)
        if spin_t_match:
            tokens["__spin_t"] = spin_t_match.group(1)

        spin_b_match = re.search(r'"__spin_b":"([^"]+)"', html)
        if spin_b_match:
            tokens["__spin_b"] = spin_b_match.group(1)

        hsi_match = re.search(r'"__hsi":"(\d+)"', html) or re.search(r'"hsi":"(\d+)"', html)
        if hsi_match:
            tokens["__hsi"] = hsi_match.group(1)

        dtsg_match = re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', html)
        if dtsg_match:
            tokens["fb_dtsg"] = dtsg_match.group(1)

        dyn_match = re.search(r'"__dyn":"([^"]+)"', html)
        if dyn_match:
            tokens["__dyn"] = dyn_match.group(1)

        csr_match = re.search(r'"__csr":"([^"]+)"', html)
        if csr_match:
            tokens["__csr"] = csr_match.group(1)

        hs_match = re.search(r'"__hs":"([^"]+)"', html)
        if hs_match:
            tokens["__hs"] = hs_match.group(1)

        hsdp_match = re.search(r'"__hsdp":"([^"]+)"', html)
        if hsdp_match:
            tokens["__hsdp"] = hsdp_match.group(1)

        hblp_match = re.search(r'"__hblp":"([^"]+)"', html)
        if hblp_match:
            tokens["__hblp"] = hblp_match.group(1)

        comet_match = re.search(r'"__comet_req":(\d+)', html)
        if comet_match:
            tokens["__comet_req"] = comet_match.group(1)

        jazoest_match = re.search(r'"jazoest["\s:]+(\d+)', html)
        if jazoest_match:
            tokens["jazoest"] = jazoest_match.group(1)

        v_match = re.search(r'"v"\s*:\s*"([a-f0-9]{4,10})"', html)
        if v_match:
            tokens["v"] = v_match.group(1)

        asbd_match = re.search(r'"asbd_id"\s*:\s*"?(\d+)"?', html) or re.search(r'x-asbd-id["\s:]+(\d+)', html)
        if asbd_match:
            tokens["x-asbd-id"] = asbd_match.group(1)

        return tokens

    def _extract_doc_ids(self, html: Optional[str]) -> dict[str, str]:
        if not html:
            return {}

        doc_ids: dict[str, str] = {}
        pattern1_matches = re.findall(
            r'__d\("(AdLibrary\w+Query)[^"]*"[^)]*\).*?["\'](\d{10,20})["\']',
            html,
        )
        for name, doc_id in pattern1_matches:
            doc_ids[name] = doc_id

        pattern2_matches = re.findall(
            r'"(?:name|operationName)"\s*:\s*"(AdLibrary\w+Query)"[^}]{0,200}"(?:queryID|id|doc_id)"\s*:\s*"(\d{10,20})"',
            html,
        )
        for name, doc_id in pattern2_matches:
            if name not in doc_ids:
                doc_ids[name] = doc_id

        pattern3_matches = re.findall(
            r'"(?:queryID|id|doc_id)"\s*:\s*"(\d{10,20})"[^}]{0,200}"(?:name|operationName)"\s*:\s*"(AdLibrary\w+Query)"',
            html,
        )
        for doc_id, name in pattern3_matches:
            if name not in doc_ids:
                doc_ids[name] = doc_id

        return doc_ids

    def _verify_tokens(self) -> None:
        if not self._tokens.get("lsd"):
            raise AuthenticationError(
                "Could not extract a real LSD token from the facebook.com homepage."
            )

        if "jazoest" not in self._tokens:
            self._tokens["jazoest"] = self._calculate_jazoest(self._tokens["lsd"])

        if "__hsi" not in self._tokens:
            self._tokens["__hsi"] = str(int(time.time() * 1000))

        if "__hs" not in self._tokens:
            self._tokens["__hs"] = "20727.HYP:comet_plat_default_pkg.2.1...0"

        if "__comet_req" not in self._tokens:
            self._tokens["__comet_req"] = "94"

        if "v" not in self._tokens:
            self._tokens["v"] = "15e849"

        if "x-asbd-id" not in self._tokens:
            self._tokens["x-asbd-id"] = "359341"

    def _generate_session_id(self) -> str:
        import uuid
        return str(uuid.uuid4())

    def _generate_collation_token(self) -> str:
        import uuid
        return str(uuid.uuid4())

    def _is_session_stale(self) -> bool:
        if not self._init_time:
            return True
        return (time.time() - self._init_time) > self._max_session_age

    def _refresh_session(self) -> bool:
        if self._consecutive_refresh_failures >= self.max_refresh_attempts:
            raise SessionExpiredError(
                f"Session refresh failed {self._consecutive_refresh_failures} consecutive times."
            )

        logger.info("Refreshing Meta Ads session...")
        self.session.close()
        self._fingerprint = generate_fingerprint()
        self.session = CffiSession(impersonate="chrome")
        self.session.headers.update(self._fingerprint.get_default_headers())
        self._tokens = {}
        self._initialized = False
        self._request_counter = 0
        self._consecutive_errors = 0

        if self._proxy_string:
            self._setup_proxy(self._proxy_string)
        self._apply_provided_cookies()

        try:
            result = self.initialize()
            if result:
                self._consecutive_refresh_failures = 0
            else:
                self._consecutive_refresh_failures += 1
            return result
        except Exception:
            self._consecutive_refresh_failures += 1
            return False

    def initialize(self) -> bool:
        logger.info("Initializing Meta Ads client via facebook.com homepage...")
        try:
            wd = f"{self._fingerprint.viewport_width}x{self._fingerprint.viewport_height}"
            self.session.cookies.set("wd", wd, domain=".facebook.com", path="/")
            self.session.cookies.set("dpr", str(self._fingerprint.dpr), domain=".facebook.com", path="/")

            init_headers = dict(self._fingerprint.get_default_headers())
            init_headers["sec-fetch-site"] = "none"

            response = self._make_request("GET", self.HOME_URL, headers=init_headers)

            if response.status_code != 200:
                raise AuthenticationError(
                    f"Homepage bootstrap failed (HTTP {response.status_code})"
                )

            self._tokens = self._extract_tokens(response.text)
            self._doc_ids = self._extract_doc_ids(response.text)

            if "__spin_t" not in self._tokens:
                self._tokens["__spin_t"] = str(int(time.time()))

            if "__spin_b" not in self._tokens:
                self._tokens["__spin_b"] = "trunk"

            if "__rev" not in self._tokens:
                rev_match = re.search(r'"server_revision":(\d+)', response.text)
                self._tokens["__rev"] = rev_match.group(1) if rev_match else FALLBACK_REV

            self._verify_tokens()

            self._initialized = True
            self._init_time = time.time()
            self._consecutive_errors = 0
            time.sleep(random.uniform(1.0, 2.0))
            return True

        except Exception as e:
            logger.error(f"Failed to initialize client: {e}")
            raise AuthenticationError(f"Failed to initialize client: {e}") from e

    def _make_request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        merged_headers = dict(self.session.headers)
        if headers:
            merged_headers.update(headers)

        last_exception: Optional[CffiRequestException] = None

        for attempt in range(self.max_retries):
            proxy_url: Optional[str] = None
            if self._proxy_pool is not None:
                proxy_url = self._proxy_pool.get_next()
                self._current_proxy = proxy_url
                self.session.proxies = self._proxy_pool.get_proxy_dict(proxy_url)

            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    headers=merged_headers,
                    timeout=self.timeout,
                    stream=stream,
                    **kwargs,
                )

                if response.status_code == 429:
                    if self._proxy_pool and proxy_url:
                        self._proxy_pool.mark_failure(proxy_url)
                    wait_time = self.retry_delay * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait_time)
                    continue

                if self._proxy_pool and proxy_url:
                    self._proxy_pool.mark_success(proxy_url)

                return response

            except CffiRequestException as e:
                last_exception = e
                if self._proxy_pool and proxy_url:
                    self._proxy_pool.mark_failure(proxy_url)
                wait_time = self.retry_delay * (2 ** attempt) + random.uniform(0, 1)
                if attempt < self.max_retries - 1:
                    time.sleep(wait_time)

        raise last_exception or CffiRequestException("Request failed after all retries")

    def _read_graphql_response(self, response: Any) -> str:
        chunks = []
        try:
            if hasattr(response, "iter_lines"):
                for line in response.iter_lines():
                    if not line:
                        continue
                    line_str = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                    chunks.append(line_str)
                    if "search_results_connection" in line_str or '"is_final":true' in line_str:
                        break
            if not chunks and hasattr(response, "text"):
                return response.text
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                response.close()
        return "\n".join(chunks)

    def _make_graphql_request(
        self,
        payload: dict[str, str],
        headers: dict[str, str],
        stream: bool = False,
    ) -> Any:
        response = self._make_request("POST", self.GRAPHQL_URL, data=payload, headers=headers, stream=stream)

        if response.status_code == 403:
            logger.warning("Got 403 on GraphQL request - refreshing session...")
            if self._refresh_session():
                lsd = self._tokens.get("lsd", "")
                payload["lsd"] = lsd
                payload["jazoest"] = self._calculate_jazoest(lsd)
                payload["__rev"] = self._tokens.get("__rev", FALLBACK_REV)
                payload["__spin_r"] = self._tokens.get("__spin_r", FALLBACK_REV)
                payload["__spin_t"] = self._tokens.get("__spin_t", str(int(time.time())))
                payload["__spin_b"] = self._tokens.get("__spin_b", "trunk")
                payload["__hsi"] = self._tokens.get("__hsi", str(int(time.time() * 1000)))

                for tok in ("__dyn", "__csr", "fb_dtsg", "__hsdp", "__hblp"):
                    if tok in self._tokens:
                        payload[tok] = self._tokens[tok]
                    else:
                        payload.pop(tok, None)

                headers["x-fb-lsd"] = lsd
                response = self._make_request("POST", self.GRAPHQL_URL, data=payload, headers=headers)

        return response

    def _calculate_jazoest(self, lsd: str) -> str:
        if not lsd:
            return "2893"
        total = sum(ord(c) for c in lsd)
        return str(2 + total)

    def _build_graphql_payload(
        self,
        doc_id: str,
        variables: dict[str, Any],
        friendly_name: str,
    ) -> dict[str, str]:
        self._request_counter += 1

        lsd = self._tokens.get("lsd", "")
        jazoest = self._tokens.get("jazoest") or self._calculate_jazoest(lsd)
        rev = self._tokens.get("__rev") or (MODERN_REV if str(doc_id) == DOC_ID_SEARCH else FALLBACK_REV)

        payload = {
            "av": "0",
            "__aaid": "0",
            "__user": "0",
            "__a": "1",
            "__req": self._encode_request_id(self._request_counter),
            "__hs": self._tokens.get("__hs", "20727.HYP:comet_plat_default_pkg.2.1...0"),
            "dpr": "2",
            "__ccg": "EXCELLENT",
            "__rev": rev,
            "__s": self._generate_short_id(),
            "__hsi": self._tokens.get("__hsi", str(int(time.time() * 1000))),
            "__comet_req": self._tokens.get("__comet_req", "94"),
            "lsd": lsd,
            "jazoest": jazoest,
            "__spin_r": rev,
            "__spin_b": "trunk",
            "__spin_t": self._tokens.get("__spin_t", str(int(time.time()))),
            "__jssesw": "1",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name,
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": doc_id,
        }

        if str(doc_id) == DOC_ID_SEARCH:
            payload["__dyn"] = self._tokens.get("__dyn", MODERN_DYN)
            payload["__csr"] = self._tokens.get("__csr", MODERN_CSR)
            payload["__hsdp"] = self._tokens.get("__hsdp", MODERN_HSDP)
            payload["__hblp"] = self._tokens.get("__hblp", MODERN_HBLP)
            payload["__sjsp"] = self._tokens.get("__sjsp", MODERN_SJSP)
        else:
            for tok in ("__dyn", "__csr", "fb_dtsg"):
                if tok in self._tokens:
                    payload[tok] = self._tokens[tok]

        return payload

    def _encode_request_id(self, counter: int) -> str:
        if counter < 10:
            return str(counter)
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        result = ""
        while counter:
            result = chars[counter % 36] + result
            counter //= 36
        return result

    def _generate_short_id(self) -> str:
        parts = ["".join(random.choices(string.ascii_lowercase + string.digits, k=6)) for _ in range(3)]
        return ":".join(parts)

    def search_ads(
        self,
        query: str = "",
        country: str = "US",
        ad_type: str = "ALL",
        active_status: str = "ACTIVE",
        media_type: str = "ALL",
        search_type: str = "KEYWORD_EXACT_PHRASE",
        page_ids: Optional[list] = None,
        cursor: Optional[str] = None,
        first: int = 10,
        sort_direction: str = "DESCENDING",
        sort_mode: Optional[str] = "SORT_BY_RELEVANCY_MONTHLY_GROUPED",
        session_id: Optional[str] = None,
        collation_token: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> tuple[dict[str, Any], Optional[str]]:
        if not self._initialized:
            self.initialize()

        if self._is_session_stale():
            logger.info("Session is stale, refreshing before request...")
            if not self._refresh_session():
                raise SessionExpiredError("Failed to refresh stale session")

        session_id = session_id or self._generate_session_id()
        collation_token = collation_token or self._generate_collation_token()

        search_doc_id = (
            self._doc_ids.get("AdLibraryMobileFocusedStateProviderRefetchQuery")
            or DOC_ID_SEARCH
        )
        is_modern = str(search_doc_id) == DOC_ID_SEARCH
        friendly_name = (
            "AdLibraryMobileFocusedStateProviderRefetchQuery"
            if is_modern
            else "AdLibrarySearchPaginationQuery"
        )

        # Force Arabic content language by default for Algerian market precision
        content_langs = ["ar"] if country == "DZ" else []

        # Build Facebook's verified startDate range object
        start_date_obj = None
        if start_date or end_date:
            start_date_obj = {}
            if start_date:
                start_date_obj["min"] = str(start_date)
            if end_date:
                start_date_obj["max"] = str(end_date)

        variables = {
            "activeStatus": active_status,
            "adType": ad_type,
            "bylines": [],
            "collationToken": collation_token,
            "contentLanguages": content_langs,
            "countries": [country],
            "excludedIDs": [],
            "isTargetedCountry": False,
            "location": None,
            "mediaType": media_type,
            "multiCountryFilterMode": None,
            "pageIDs": page_ids or [],
            "potentialReachInput": [],
            "publisherPlatforms": [],
            "queryString": query,
            "regions": [],
            "searchType": search_type,
            "sessionID": session_id,
            "source": None,
            "startDate": start_date_obj,
            "v": self._tokens.get("v", "15e849"),
            "viewAllPageID": "0",
        }

        if is_modern:
            variables.update({
                "audienceTimeframe": "LAST_7_DAYS",
                "country": country,
                "deeplinkAdID": None,
                "fetchPageInfo": False,
                "fetchSharedDisclaimers": False,
                "hasDeeplinkAdID": False,
                "isAboutTab": False,
                "isAudienceTab": False,
                "isLandingPage": False,
            })
        else:
            variables["first"] = first

        # Sorting: strictly enforce newest to oldest (relevancy_monthly_grouped descending)
        if sort_mode == "SORT_BY_TOTAL_IMPRESSIONS":
            variables["sortData"] = {
                "direction": sort_direction,
                "mode": sort_mode,
            }
        else:
            variables["sortData"] = {
                "direction": "DESCENDING",
                "mode": "SORT_BY_RELEVANCY_MONTHLY_GROUPED",
            }

        if cursor:
            variables["cursor"] = cursor

        payload = self._build_graphql_payload(
            doc_id=search_doc_id,
            variables=variables,
            friendly_name=friendly_name,
        )

        ad_type_url = {
            "ALL": "all",
            "POLITICAL_AND_ISSUE_ADS": "political_and_issue_ads",
            "HOUSING_ADS": "housing",
            "EMPLOYMENT_ADS": "employment",
            "CREDIT_ADS": "credit",
        }.get(ad_type, "all")

        headers = dict(self._fingerprint.get_graphql_headers())
        headers["x-fb-friendly-name"] = friendly_name
        headers["x-fb-lsd"] = self._tokens.get("lsd", "")
        if "x-asbd-id" in self._tokens:
            headers["x-asbd-id"] = self._tokens["x-asbd-id"]

        search_type_url = search_type.lower()
        media_type_url = media_type.lower()
        referer_url = (
            f"{self.AD_LIBRARY_URL}?active_status={active_status.lower()}"
            f"&ad_type={ad_type_url}&content_languages[0]=ar&country={country}&is_targeted_country=false"
            f"&media_type={media_type_url}&q={quote(query)}&search_type={search_type_url}"
            f"&sort_data[mode]=relevancy_monthly_grouped&sort_data[direction]=desc"
        )
        if start_date:
            referer_url += f"&start_date[min]={start_date}"
        if end_date:
            referer_url += f"&start_date[max]={end_date}"
        headers["referer"] = referer_url

        response = self._make_graphql_request(payload, headers, stream=is_modern)

        if response.status_code != 200:
            raise MetaAdsError(f"GraphQL request failed with status {response.status_code}")

        try:
            text = self._read_graphql_response(response) if is_modern else response.text
            data = self._parse_raw_graphql_text(text)

            if "errors" in data:
                errors = data.get("errors", [])
                for error in errors:
                    error_code = error.get("code")
                    error_msg = error.get("message", "Unknown error")

                    if error_code == 1675004 or "rate limit" in error_msg.lower():
                        self._consecutive_errors += 1
                        time.sleep(5 + random.uniform(0, 3))
                        return {"ads": [], "page_info": {}, "rate_limited": True, "error": error_msg}, None

                    if error_code in (1357004, 1357001) or "session" in error_msg.lower():
                        self._consecutive_errors += 1
                        if self._consecutive_errors >= 2:
                            self._refresh_session()
                        return {"ads": [], "page_info": {}, "session_expired": True, "error": error_msg}, None

                if not data.get("data"):
                    return {"ads": [], "page_info": {}, "error": str(errors)}, None

            self._consecutive_errors = 0
            self._consecutive_refresh_failures = 0
            return self._parse_search_response(data)

        except json.JSONDecodeError as e:
            raise

    @staticmethod
    def _parse_raw_graphql_text(text: str) -> dict[str, Any]:
        if not text:
            return {}

        clean_text = text.strip()
        if clean_text.startswith("for (;;);"):
            clean_text = clean_text[9:].strip()

        decoder = json.JSONDecoder()
        idx = 0
        chunks: list[dict[str, Any]] = []

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

        for chunk in chunks:
            chunk_data = chunk.get("data", {}) if isinstance(chunk, dict) else {}
            if isinstance(chunk_data, dict) and ("ad_library_main" in chunk_data or "adLibraryMain" in chunk_data):
                return chunk

        try:
            return json.loads(clean_text)
        except Exception:
            return chunks[0] if chunks else {}

    def _parse_search_response(self, data: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
        try:
            results = (
                data.get("data", {})
                .get("ad_library_main", {})
                .get("search_results_connection", {})
            )

            if not results:
                results = (
                    data.get("data", {})
                    .get("adLibraryMain", {})
                    .get("searchResultsConnection", {})
                )

            if not results:
                results = data.get("data", {})

            edges = results.get("edges", [])
            page_info = results.get("page_info", {}) or results.get("pageInfo", {})

            next_cursor = None
            if page_info.get("has_next_page") or page_info.get("hasNextPage"):
                next_cursor = page_info.get("end_cursor") or page_info.get("endCursor")

            total_count = results.get("count") or results.get("total_count")

            ads = []
            for edge in edges:
                node = edge.get("node", edge)
                if node:
                    collated = node.get("collated_results", [])
                    for ad_data in collated:
                        snapshot = ad_data.get("snapshot") or {}
                        flattened = dict(ad_data)
                        if snapshot:
                            for key, value in snapshot.items():
                                if key not in flattened or flattened[key] is None or flattened[key] == "":
                                    flattened[key] = value
                        ads.append(flattened)

            return {"ads": ads, "page_info": page_info, "total_count": total_count, "raw": data}, next_cursor

        except Exception as e:
            return {"ads": [], "page_info": {}, "raw": data, "error": str(e)}, None

    def search_pages(self, query: str, country: str = "US") -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()

        variables = {
            "queryString": query,
            "country": country,
            "adType": "ALL",
            "isMobile": False,
        }

        typeahead_doc_id = self._doc_ids.get(
            "useAdLibraryTypeaheadSuggestionDataSourceQuery", DOC_ID_TYPEAHEAD
        )
        payload = self._build_graphql_payload(
            doc_id=typeahead_doc_id,
            variables=variables,
            friendly_name="useAdLibraryTypeaheadSuggestionDataSourceQuery",
        )

        headers = dict(self._fingerprint.get_graphql_headers())
        headers["x-fb-friendly-name"] = "useAdLibraryTypeaheadSuggestionDataSourceQuery"
        headers["x-fb-lsd"] = self._tokens.get("lsd", "")
        headers["referer"] = f"{self.AD_LIBRARY_URL}?active_status=all&ad_type=all&country={country}&q={quote(query)}"

        try:
            response = self._make_graphql_request(payload, headers)
            if response.status_code != 200:
                return []
            data = self._parse_raw_graphql_text(response.text)
            return self._parse_typeahead_response(data)
        except Exception:
            return []

    def _parse_typeahead_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            raw_suggestions = (
                data.get("data", {})
                .get("ad_library_main", {})
                .get("typeahead_suggestions")
            )

            suggestions: list[dict[str, Any]] = []
            if isinstance(raw_suggestions, dict):
                suggestions = raw_suggestions.get("page_results", [])
            elif isinstance(raw_suggestions, list):
                suggestions = raw_suggestions

            if not suggestions:
                raw_alt = data.get("data", {}).get("adLibraryMain", {}).get("typeaheadSuggestions")
                if isinstance(raw_alt, dict):
                    suggestions = raw_alt.get("page_results", []) or raw_alt.get("pageResults", [])
                elif isinstance(raw_alt, list):
                    suggestions = raw_alt

            pages: list[dict[str, Any]] = []
            for item in suggestions:
                page = {
                    "page_id": str(item.get("page_id", "") or item.get("pageID", "")),
                    "page_name": item.get("page_name", "") or item.get("pageName", "") or item.get("name", ""),
                    "page_profile_uri": item.get("page_profile_uri") or item.get("pageProfileURI") or item.get("page_url") or "",
                    "page_alias": item.get("page_alias") or item.get("pageAlias"),
                    "page_logo_url": item.get("page_profile_picture_url") or item.get("pageProfilePictureURL") or item.get("profile_picture_url"),
                    "page_verified": item.get("is_verified") or item.get("isVerified"),
                    "page_like_count": item.get("page_like_count") or item.get("pageLikeCount"),
                    "category": item.get("category") or item.get("page_category"),
                }
                if page["page_id"]:
                    pages.append(page)
            return pages
        except Exception:
            return []

    def get_ad_details(self, ad_archive_id: str, page_id: Optional[str] = None) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()

        if page_id:
            try:
                response_data, _ = self.search_ads(
                    query="",
                    search_type="PAGE",
                    page_ids=[page_id],
                    first=30,
                    active_status="ALL",
                    country="ALL",
                )
                for ad_data in response_data.get("ads", []):
                    found_id = str(ad_data.get("ad_archive_id") or ad_data.get("id") or "")
                    if found_id == str(ad_archive_id):
                        return dict(ad_data)
            except Exception:
                pass

        raise NotImplementedError(f"Could not retrieve detail data for ad {ad_archive_id}.")

    def close(self) -> None:
        self.session.close()
        self._initialized = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()