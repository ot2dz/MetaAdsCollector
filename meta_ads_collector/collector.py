# -*- coding: utf-8 -*-
"""
Meta Ads Library Collector

High-level interface for collecting ads from the Meta Ad Library.
Handles pagination, rate limiting, and data storage.
"""

import csv
import json
import logging
import random
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .client import MetaAdsClient
from .constants import (
    AD_TYPE_ALL,
    AD_TYPE_CREDIT,
    AD_TYPE_EMPLOYMENT,
    AD_TYPE_HOUSING,
    AD_TYPE_POLITICAL,
    DEFAULT_JITTER,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PAGE_SIZE,
    DEFAULT_RATE_LIMIT_DELAY,
    DEFAULT_TIMEOUT,
    SEARCH_EXACT,
    SEARCH_KEYWORD,
    SEARCH_PAGE,
    SEARCH_UNORDERED,
    SORT_IMPRESSIONS,
    SORT_RELEVANCY,
    STATUS_ACTIVE,
    STATUS_ALL,
    STATUS_INACTIVE,
    VALID_AD_TYPES,
    VALID_SEARCH_TYPES,
    VALID_SORT_MODES,
    VALID_STATUSES,
)
from .dedup import DeduplicationTracker
from .events import (
    AD_COLLECTED,
    COLLECTION_FINISHED,
    COLLECTION_STARTED,
    ERROR_OCCURRED,
    PAGE_FETCHED,
    RATE_LIMITED,
    SESSION_REFRESHED,
    EventEmitter,
)
from .exceptions import InvalidParameterError
from .filters import FilterConfig, passes_filter
from .media import MediaDownloader, MediaDownloadResult
from .models import Ad, PageInfo, PageSearchResult
from .proxy_pool import ProxyPool
from .url_parser import extract_page_id_from_url

logger = logging.getLogger(__name__)

class MetaAdsCollector:
    """
    High-level collector for Meta Ad Library ads.

    Provides an easy-to-use interface for searching and collecting ads
    with automatic pagination, rate limiting, and multiple export formats.
    """

    AD_TYPE_ALL = AD_TYPE_ALL
    AD_TYPE_POLITICAL = AD_TYPE_POLITICAL
    AD_TYPE_HOUSING = AD_TYPE_HOUSING
    AD_TYPE_EMPLOYMENT = AD_TYPE_EMPLOYMENT
    AD_TYPE_CREDIT = AD_TYPE_CREDIT

    STATUS_ACTIVE = STATUS_ACTIVE
    STATUS_INACTIVE = STATUS_INACTIVE
    STATUS_ALL = STATUS_ALL

    SEARCH_KEYWORD = SEARCH_KEYWORD
    SEARCH_EXACT = SEARCH_EXACT
    SEARCH_UNORDERED = SEARCH_UNORDERED
    SEARCH_PAGE = SEARCH_PAGE

    SORT_RELEVANCY = SORT_RELEVANCY
    SORT_IMPRESSIONS = SORT_IMPRESSIONS
    SORT_DATE = None

    def __init__(
        self,
        proxy: Optional[Union[str, list[str], ProxyPool]] = None,
        rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY,
        jitter: float = DEFAULT_JITTER,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        callbacks: Optional[dict[str, Callable]] = None,
        cookies: Optional[Union[dict, str]] = None,
    ):
        self.client = MetaAdsClient(
            proxy=proxy,
            timeout=timeout,
            max_retries=max_retries,
            cookies=cookies,
        )
        self.rate_limit_delay = rate_limit_delay
        self.jitter = jitter

        self.event_emitter = EventEmitter()
        if callbacks:
            for event_type, cb in callbacks.items():
                self.event_emitter.on(event_type, cb)

        self.stats: dict[str, Any] = {
            "requests_made": 0,
            "ads_collected": 0,
            "pages_fetched": 0,
            "errors": 0,
            "start_time": None,
            "end_time": None,
        }

    def search_pages(
        self,
        query: str,
        country: str = "US",
    ) -> list[PageSearchResult]:
        raw_pages = self.client.search_pages(query=query, country=country)

        results: list[PageSearchResult] = []
        for page_data in raw_pages:
            try:
                result = PageSearchResult(
                    page_id=page_data.get("page_id", ""),
                    page_name=page_data.get("page_name", ""),
                    page_profile_uri=page_data.get("page_profile_uri"),
                    page_alias=page_data.get("page_alias"),
                    page_logo_url=page_data.get("page_logo_url"),
                    page_verified=page_data.get("page_verified"),
                    page_like_count=page_data.get("page_like_count"),
                    category=page_data.get("category"),
                )
                if result.page_id:
                    results.append(result)
            except Exception as exc:
                logger.warning("Failed to parse page search result: %s", exc)
                continue

        return results

    def collect_by_page_id(
        self,
        page_id: str,
        **kwargs: Any,
    ) -> Iterator[Ad]:
        kwargs.setdefault("search_type", SEARCH_PAGE)
        kwargs["page_ids"] = [page_id]
        yield from self.search(**kwargs)

    def collect_by_page_url(
        self,
        url: str,
        **kwargs: Any,
    ) -> Iterator[Ad]:
        page_id = extract_page_id_from_url(url)
        if not page_id:
            logger.warning(
                "Could not extract page ID from URL: %s. "
                "If this is a vanity URL, use search_pages() to resolve it first.",
                url,
            )
            return
        yield from self.collect_by_page_id(page_id, **kwargs)

    def collect_by_page_name(
        self,
        page_name: str,
        country: str = "US",
        **kwargs: Any,
    ) -> Iterator[Ad]:
        pages = self.search_pages(query=page_name, country=country)
        if not pages:
            logger.warning("No pages found for name: %s", page_name)
            return
        best = pages[0]
        logger.info(
            "Resolved page name %r to page ID %s (%s)",
            page_name,
            best.page_id,
            best.page_name,
        )
        kwargs.setdefault("country", country)
        yield from self.collect_by_page_id(best.page_id, **kwargs)

    def _delay(self) -> None:
        delay = self.rate_limit_delay + random.uniform(0, self.jitter)
        time.sleep(delay)

    @staticmethod
    def _validate_params(
        ad_type: str,
        status: str,
        search_type: str,
        sort_by: Optional[str],
        country: str,
    ) -> None:
        if ad_type not in VALID_AD_TYPES:
            raise InvalidParameterError("ad_type", ad_type, VALID_AD_TYPES)
        if status not in VALID_STATUSES:
            raise InvalidParameterError("status", status, VALID_STATUSES)
        if search_type not in VALID_SEARCH_TYPES:
            raise InvalidParameterError("search_type", search_type, VALID_SEARCH_TYPES)
        if sort_by not in VALID_SORT_MODES:
            raise InvalidParameterError("sort_by", sort_by, VALID_SORT_MODES)
        if not country or len(country) != 2 or not country.isalpha():
            raise InvalidParameterError(
                "country", country, "a 2-letter ISO 3166-1 alpha-2 code (e.g. 'US', 'EG')"
            )

    def search(
        self,
        query: str = "",
        country: str = "US",
        ad_type: str = AD_TYPE_ALL,
        status: str = STATUS_ACTIVE,
        search_type: str = SEARCH_KEYWORD,
        page_ids: Optional[list[str]] = None,
        sort_by: Optional[str] = SORT_IMPRESSIONS,
        max_results: Optional[int] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        filter_config: Optional[FilterConfig] = None,
        dedup_tracker: Optional[DeduplicationTracker] = None,
        max_consecutive_seen: Optional[int] = None,
        stop_event: Optional[Any] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Iterator[Ad]:
        import uuid

        country = country.upper()
        self._validate_params(ad_type, status, search_type, sort_by, country)

        self.stats["start_time"] = datetime.now(timezone.utc)
        cursor = None
        collected = 0
        page_number = 0
        consecutive_seen = 0
        consecutive_older = 0
        early_exit_triggered = False
        search_start_time = time.monotonic()

        search_session_id = str(uuid.uuid4())
        search_collation_token = str(uuid.uuid4())

        # Extract dates prioritizing directly passed start_date/end_date
        api_start_date = start_date
        api_end_date = end_date
        if filter_config:
            if not api_start_date and filter_config.start_date:
                api_start_date = filter_config.start_date.strftime("%Y-%m-%d")
            if not api_end_date and filter_config.end_date:
                api_end_date = filter_config.end_date.strftime("%Y-%m-%d")

        logger.info(f"Starting search: query='{query}', country={country}, ad_type={ad_type}")

        self.event_emitter.emit(COLLECTION_STARTED, {
            "query": query,
            "country": country,
            "ad_type": ad_type,
            "status": status,
            "search_type": search_type,
            "page_ids": page_ids,
            "max_results": max_results,
        })

        try:
            while True:
                if stop_event and stop_event.is_set():
                    logger.info("Search interrupted by stop event signal.")
                    break

                # Only stop if max_results is explicitly provided and greater than 0
                if max_results and max_results > 0 and collected >= max_results:
                    logger.info(f"Reached user-defined max_results limit: {max_results}")
                    break

                retry_count = 0
                max_retries = 3
                response = None

                while retry_count < max_retries:
                    if stop_event and stop_event.is_set():
                        break
                    try:
                        self.stats["requests_made"] += 1
                        response, next_cursor = self.client.search_ads(
                            query=query,
                            country=country,
                            ad_type=ad_type,
                            active_status=status,
                            search_type=search_type,
                            page_ids=page_ids,
                            cursor=cursor,
                            first=page_size,
                            sort_mode=sort_by,
                            session_id=search_session_id,
                            collation_token=search_collation_token,
                            start_date=api_start_date,
                            end_date=api_end_date,
                        )

                        if response.get("rate_limited"):
                            retry_count += 1
                            wait_time = 5 * retry_count + random.uniform(1, 3)
                            self.event_emitter.emit(RATE_LIMITED, {
                                "wait_seconds": wait_time,
                                "retry_count": retry_count,
                            })
                            if retry_count < max_retries:
                                time.sleep(wait_time)
                                continue
                            else:
                                self.stats["errors"] += 1
                                self.event_emitter.emit(ERROR_OCCURRED, {
                                    "exception": None,
                                    "context": "Max retries exceeded due to rate limiting",
                                })
                                return

                        if response.get("session_expired"):
                            retry_count += 1
                            self.event_emitter.emit(SESSION_REFRESHED, {
                                "reason": "session_expired",
                            })
                            if retry_count < max_retries:
                                time.sleep(2)
                                continue
                            else:
                                self.stats["errors"] += 1
                                self.event_emitter.emit(ERROR_OCCURRED, {
                                    "exception": None,
                                    "context": "Max retries exceeded due to session expiry",
                                })
                                return

                        self.stats["pages_fetched"] += 1
                        page_number += 1
                        break

                    except Exception as e:
                        logger.error(f"Search request failed: {e}")
                        self.stats["errors"] += 1
                        self.event_emitter.emit(ERROR_OCCURRED, {
                            "exception": e,
                            "context": f"Search request failed on retry {retry_count + 1}",
                        })
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        time.sleep(3 * retry_count)

                if response is None:
                    break

                ads_data = response.get("ads", [])
                if not ads_data:
                    logger.info("No more results returned")
                    break

                has_next = bool(next_cursor)
                total_count = response.get("total_count")
                self.event_emitter.emit(PAGE_FETCHED, {
                    "page_number": page_number,
                    "ads_on_page": len(ads_data),
                    "total_available": total_count,
                    "has_next_page": has_next,
                })

                for ad_data in ads_data:
                    if max_results and collected >= max_results:
                        break

                    try:
                        ad = Ad.from_graphql_response(ad_data)

                        if dedup_tracker is not None and dedup_tracker.has_seen(ad.id):
                            consecutive_seen += 1
                            if max_consecutive_seen and consecutive_seen >= max_consecutive_seen:
                                logger.info(
                                    f"Early exit: encountered {consecutive_seen} consecutive previously collected ads. Stopping."
                                )
                                early_exit_triggered = True
                                break
                            continue
                        else:
                            consecutive_seen = 0

                        # Smart Cut-off: Stop collecting when encountering ads older than requested start date
                        if filter_config and filter_config.start_date and ad.delivery_start_time:
                            if ad.delivery_start_time.date() < filter_config.start_date.date():
                                consecutive_older += 1
                                if consecutive_older >= 5:
                                    logger.info(f"⚡ تم الوصول للحد الأدنى من التاريخ المطلوب ({filter_config.start_date.date()}). إيقاف السحب بنجاح.")
                                    early_exit_triggered = True
                                    break
                                continue
                            else:
                                consecutive_older = 0

                        if filter_config and filter_config.end_date and ad.delivery_start_time:
                            if ad.delivery_start_time.date() > filter_config.end_date.date():
                                continue

                        if filter_config is not None and not passes_filter(ad, filter_config):
                            continue

                        collected += 1
                        self.stats["ads_collected"] += 1

                        if progress_callback:
                            progress_callback(collected, max_results or -1)

                        self.event_emitter.emit(AD_COLLECTED, {"ad": ad})
                        yield ad

                        if dedup_tracker is not None:
                            dedup_tracker.mark_seen(ad.id)

                    except Exception as e:
                        logger.warning(f"Failed to parse ad: {e}")
                        self.stats["errors"] += 1
                        self.event_emitter.emit(ERROR_OCCURRED, {
                            "exception": e,
                            "context": "Failed to parse ad from response",
                        })
                        continue

                if early_exit_triggered:
                    break

                # Continue fetching next pages until Facebook has no more cursors (Full Drain)
                if not next_cursor or not has_next:
                    logger.info(f"Full Drain Completed: All available pages fetched from Facebook. Total raw collected: {collected}")
                    break

                cursor = next_cursor
                logger.info(f"Advancing to next page via cursor: {cursor[:20]}... (Total collected so far: {collected})")
                self._delay()

        finally:
            self.stats["end_time"] = datetime.now(timezone.utc)
            duration = time.monotonic() - search_start_time
            if dedup_tracker is not None:
                dedup_tracker.update_collection_time()
                dedup_tracker.save()
            logger.info(f"Search completed: {collected} ads collected")
            self.event_emitter.emit(COLLECTION_FINISHED, {
                "total_ads": collected,
                "total_pages": page_number,
                "duration_seconds": duration,
                "early_exit": early_exit_triggered,
            })

    def stream(
        self,
        query: str = "",
        country: str = "US",
        ad_type: str = AD_TYPE_ALL,
        status: str = STATUS_ACTIVE,
        search_type: str = SEARCH_KEYWORD,
        page_ids: Optional[list[str]] = None,
        sort_by: Optional[str] = SORT_IMPRESSIONS,
        max_results: Optional[int] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        filter_config: Optional[FilterConfig] = None,
        dedup_tracker: Optional[DeduplicationTracker] = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        import queue as _queue
        from .events import ALL_EVENT_TYPES, COLLECTION_FINISHED, Event

        _SENTINEL = object()
        event_queue: _queue.Queue[Any] = _queue.Queue()

        def _listener(event: Event) -> None:
            event_queue.put((event.event_type, event.data))
            if event.event_type == COLLECTION_FINISHED:
                event_queue.put(_SENTINEL)

        for et in ALL_EVENT_TYPES:
            self.event_emitter.on(et, _listener)

        try:
            search_iter = self.search(
                query=query,
                country=country,
                ad_type=ad_type,
                status=status,
                search_type=search_type,
                page_ids=page_ids,
                sort_by=sort_by,
                max_results=max_results,
                page_size=page_size,
                filter_config=filter_config,
                dedup_tracker=dedup_tracker,
            )

            for _ad in search_iter:
                while not event_queue.empty():
                    item = event_queue.get_nowait()
                    if item is _SENTINEL:
                        return
                    yield item

            while not event_queue.empty():
                item = event_queue.get_nowait()
                if item is _SENTINEL:
                    return
                yield item

        finally:
            for et in ALL_EVENT_TYPES:
                self.event_emitter.off(et, _listener)

    def collect_with_media(
        self,
        media_output_dir: Union[str, Path] = "./ad_media",
        query: str = "",
        country: str = "US",
        ad_type: str = AD_TYPE_ALL,
        status: str = STATUS_ACTIVE,
        search_type: str = SEARCH_KEYWORD,
        page_ids: Optional[list[str]] = None,
        sort_by: Optional[str] = SORT_IMPRESSIONS,
        max_results: Optional[int] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        filter_config: Optional[FilterConfig] = None,
        dedup_tracker: Optional[DeduplicationTracker] = None,
    ) -> Iterator[tuple[Ad, list[MediaDownloadResult]]]:
        downloader = MediaDownloader(
            output_dir=media_output_dir,
            session=self.client.session,
        )

        for ad in self.search(
            query=query,
            country=country,
            ad_type=ad_type,
            status=status,
            search_type=search_type,
            page_ids=page_ids,
            sort_by=sort_by,
            max_results=max_results,
            page_size=page_size,
            progress_callback=progress_callback,
            filter_config=filter_config,
            dedup_tracker=dedup_tracker,
        ):
            try:
                results = downloader.download_ad_media(ad)
            except Exception as exc:
                logger.warning(
                    "Unexpected media download failure for ad %s: %s",
                    ad.id, exc,
                )
                results = []
            yield ad, results

    def download_ad_media(
        self,
        ad: Ad,
        output_dir: Union[str, Path] = "./ad_media",
    ) -> list[MediaDownloadResult]:
        try:
            downloader = MediaDownloader(
                output_dir=output_dir,
                session=self.client.session,
            )
            return downloader.download_ad_media(ad)
        except Exception as exc:
            logger.warning(
                "Failed to download media for ad %s: %s", ad.id, exc,
            )
            return []

    def enrich_ad(self, ad: Ad) -> Ad:
        try:
            page_id = ad.page.id if ad.page else None
            detail_data = self.client.get_ad_details(
                ad_archive_id=ad.id,
                page_id=page_id,
            )
        except NotImplementedError:
            return ad
        except Exception as exc:
            logger.warning("Failed to fetch details for ad %s: %s", ad.id, exc)
            return ad

        try:
            enriched = Ad.from_graphql_response(detail_data)
            import copy
            result = copy.deepcopy(ad)

            if enriched.page and result.page and not result.page.profile_picture_url and enriched.page.profile_picture_url:
                result.page = PageInfo(
                    id=result.page.id,
                    name=result.page.name,
                    profile_picture_url=enriched.page.profile_picture_url,
                    page_url=result.page.page_url or enriched.page.page_url,
                    likes=result.page.likes or enriched.page.likes,
                    verified=result.page.verified or enriched.page.verified,
                )

            if not result.ad_library_id and enriched.ad_library_id:
                result.ad_library_id = enriched.ad_library_id
            if not result.snapshot_url and enriched.snapshot_url:
                result.snapshot_url = enriched.snapshot_url
            if not result.ad_snapshot_url and enriched.ad_snapshot_url:
                result.ad_snapshot_url = enriched.ad_snapshot_url

            return result

        except Exception as exc:
            logger.warning("Failed to merge detail data for ad %s: %s", ad.id, exc)
            return ad

    def collect(
        self,
        query: str = "",
        country: str = "US",
        ad_type: str = AD_TYPE_ALL,
        status: str = STATUS_ACTIVE,
        search_type: str = SEARCH_KEYWORD,
        page_ids: Optional[list[str]] = None,
        sort_by: Optional[str] = SORT_IMPRESSIONS,
        max_results: Optional[int] = None,
        page_size: int = 10,
        filter_config: Optional[FilterConfig] = None,
        dedup_tracker: Optional[DeduplicationTracker] = None,
        max_consecutive_seen: Optional[int] = None,
    ) -> list[Ad]:
        return list(self.search(
            query=query,
            country=country,
            ad_type=ad_type,
            status=status,
            search_type=search_type,
            page_ids=page_ids,
            sort_by=sort_by,
            max_results=max_results,
            page_size=page_size,
            filter_config=filter_config,
            dedup_tracker=dedup_tracker,
            max_consecutive_seen=max_consecutive_seen,
        ))

    def collect_to_json(
        self,
        output_path: str,
        query: str = "",
        country: str = "US",
        ad_type: str = AD_TYPE_ALL,
        status: str = STATUS_ACTIVE,
        search_type: str = SEARCH_KEYWORD,
        page_ids: Optional[list[str]] = None,
        sort_by: Optional[str] = SORT_IMPRESSIONS,
        max_results: Optional[int] = None,
        page_size: int = 10,
        include_raw: bool = False,
        indent: int = 2,
        filter_config: Optional[FilterConfig] = None,
        dedup_tracker: Optional[DeduplicationTracker] = None,
    ) -> int:
        ads = []
        for ad in self.search(
            query=query,
            country=country,
            ad_type=ad_type,
            status=status,
            search_type=search_type,
            page_ids=page_ids,
            sort_by=sort_by,
            max_results=max_results,
            page_size=page_size,
            filter_config=filter_config,
            dedup_tracker=dedup_tracker,
        ):
            ads.append(ad.to_dict(include_raw=include_raw))

        output = {
            "metadata": {
                "query": query,
                "country": country,
                "total_count": len(ads),
                "collected_at": datetime.now(timezone.utc).isoformat(),
            },
            "ads": ads,
        }

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=indent, ensure_ascii=False)

        return len(ads)

    def collect_to_csv(
        self,
        output_path: str,
        query: str = "",
        country: str = "US",
        ad_type: str = AD_TYPE_ALL,
        status: str = STATUS_ACTIVE,
        search_type: str = SEARCH_KEYWORD,
        page_ids: Optional[list[str]] = None,
        sort_by: Optional[str] = SORT_IMPRESSIONS,
        max_results: Optional[int] = None,
        page_size: int = 10,
        filter_config: Optional[FilterConfig] = None,
        dedup_tracker: Optional[DeduplicationTracker] = None,
    ) -> int:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        columns = [
            "id", "page_id", "page_name", "page_url", "is_active",
            "ad_status", "delivery_start_time", "delivery_stop_time",
            "creative_body", "creative_title", "creative_description",
            "creative_link_url", "creative_image_url", "snapshot_url",
            "impressions_lower", "impressions_upper", "spend_lower",
            "spend_upper", "currency", "publisher_platforms", "languages",
            "funding_entity", "disclaimer", "ad_type", "collected_at",
        ]

        count = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()

            for ad in self.search(
                query=query,
                country=country,
                ad_type=ad_type,
                status=status,
                search_type=search_type,
                page_ids=page_ids,
                sort_by=sort_by,
                max_results=max_results,
                page_size=page_size,
                filter_config=filter_config,
                dedup_tracker=dedup_tracker,
            ):
                primary_creative = ad.creatives[0] if ad.creatives else None
                row = {
                    "id": ad.id,
                    "page_id": ad.page.id if ad.page else "",
                    "page_name": ad.page.name if ad.page else "",
                    "page_url": ad.page.page_url if ad.page else "",
                    "is_active": ad.is_active if ad.is_active is not None else "",
                    "ad_status": ad.ad_status or "",
                    "delivery_start_time": ad.delivery_start_time.isoformat() if ad.delivery_start_time else "",
                    "delivery_stop_time": ad.delivery_stop_time.isoformat() if ad.delivery_stop_time else "",
                    "creative_body": primary_creative.body if primary_creative else "",
                    "creative_title": primary_creative.title if primary_creative else "",
                    "creative_description": primary_creative.description if primary_creative else "",
                    "creative_link_url": primary_creative.link_url if primary_creative else "",
                    "creative_image_url": primary_creative.image_url if primary_creative else "",
                    "snapshot_url": ad.snapshot_url or ad.ad_snapshot_url or "",
                    "impressions_lower": ad.impressions.lower_bound if ad.impressions else "",
                    "impressions_upper": ad.impressions.upper_bound if ad.impressions else "",
                    "spend_lower": ad.spend.lower_bound if ad.spend else "",
                    "spend_upper": ad.spend.upper_bound if ad.spend else "",
                    "currency": ad.currency or "",
                    "publisher_platforms": ",".join(ad.publisher_platforms),
                    "languages": ",".join(ad.languages),
                    "funding_entity": ad.funding_entity or "",
                    "disclaimer": ad.disclaimer or "",
                    "ad_type": ad.ad_type or "",
                    "collected_at": ad.collected_at.isoformat(),
                }
                writer.writerow(row)
                count += 1

        return count

    def get_stats(self) -> dict[str, Any]:
        stats = self.stats.copy()
        if stats["start_time"] and stats["end_time"]:
            duration = (stats["end_time"] - stats["start_time"]).total_seconds()
            stats["duration_seconds"] = duration
            if duration > 0:
                stats["ads_per_second"] = stats["ads_collected"] / duration
        return stats

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()