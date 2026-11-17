from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo as _tzinfo
from pathlib import Path
from threading import Lock, Thread

import urllib3

from .coverage import (
    CALENDAR_DATA_VERSION,
    CoverageResult,
    CoverageStatus,
    failed_coverage,
)
from .icalparser import Event, ParseResult, now, parse_events
from .icaldownload import ICalDownload


# Lock for event data
event_lock = Lock()
# Event data
event_store: dict[str, list[Event]] = {}
# Coverage aware cache entries
result_store: dict[str, CachedResult] = {}
# Threads
threads: dict[str, list[Thread]] = {}


@dataclass(frozen=True)
class CachedResult:
    """
    Cache entry for an asynchronous calendar request.

    :param events: events returned by the request
    :param coverage: coverage status of the query window
    :param version: data format version the result was produced with
    :param fetched_at: time the result was fetched
    """

    events: list[Event]
    coverage: CoverageResult
    version: int
    fetched_at: datetime

    @property
    def stale(self) -> bool:
        return self.version != CALENDAR_DATA_VERSION or self.coverage.stale


def events(
    url: str | None = None,
    file: str | Path | None = None,
    string_content: bytes | str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    fix_apple: bool = False,
    http: urllib3.PoolManager | None = None,
    tzinfo: _tzinfo | None = None,
    sort: bool = False,
    strict: bool = False,
) -> list[Event]:
    """
    Get all events form the given iCal URL occurring in the given time range.

    :param url: iCal URL
    :param file: iCal file path
    :param string_content: iCal content as string
    :param start: start date (see datetime.date)
    :param end: end date (see datetime.date)
    :param fix_apple: fix known Apple iCal issues
    :param tzinfo: return values in specified tz
    :param sort: sort return values
    :param strict: return dates, datetimes and datetime with timezones as specified in ical
    :sort sorts events by start time

    :return events
    """
    found_events = []

    content = ""
    ical_download = ICalDownload(http=http)

    if url:
        content = ical_download.data_from_url(url, apple_fix=fix_apple)

    if not content and file:
        content = ical_download.data_from_file(file, apple_fix=fix_apple)

    if not content and string_content:
        content = ical_download.data_from_string(string_content, apple_fix=fix_apple)

    found_events += parse_events(
        content, start=start, end=end, tzinfo=tzinfo, sort=sort, strict=strict
    )

    if sort:
        found_events.sort()

    return found_events


def events_with_coverage(
    url: str | None = None,
    file: str | Path | None = None,
    string_content: bytes | str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    fix_apple: bool = False,
    http: urllib3.PoolManager | None = None,
    tzinfo: _tzinfo | None = None,
    sort: bool = False,
    strict: bool = False,
) -> ParseResult:
    """
    Get events together with the coverage status of the query window.

    Download or parsing failures never raise: they produce a ParseResult with
    failed coverage and an empty event list, so callers can never mistake a
    failed collection for an empty calendar.

    :return: ParseResult with events and coverage status
    """
    window_start = start or now()
    window_end = end or window_start + timedelta(days=7)

    try:
        content = ""
        ical_download = ICalDownload(http=http)

        if url:
            content = ical_download.data_from_url(url, apple_fix=fix_apple)

        if not content and file:
            content = ical_download.data_from_file(file, apple_fix=fix_apple)

        if not content and string_content:
            content = ical_download.data_from_string(
                string_content, apple_fix=fix_apple
            )

        return parse_events(
            content,
            start=start,
            end=end,
            tzinfo=tzinfo,
            sort=sort,
            strict=strict,
            coverage=True,
        )
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        return ParseResult([], failed_coverage(window_start, window_end, reason))


def request_data(
    key: str,
    url: str | None,
    file: str | Path | None,
    string_content: bytes | str | None,
    start: datetime | None,
    end: datetime | None,
    fix_apple: bool,
) -> None:
    """
    Request data, update local data cache and remove this Thread from queue.

    A failed request keeps any previously cached events but marks the coverage
    as failed and stale instead of overwriting them with an empty result.

    :param key: key for data source to get result later
    :param url: iCal URL
    :param file: iCal file path
    :param string_content: iCal content as string
    :param start: start date
    :param end: end date
    :param fix_apple: fix known Apple iCal issues
    """
    result = events_with_coverage(
        url=url,
        file=file,
        string_content=string_content,
        start=start,
        end=end,
        fix_apple=fix_apple,
    )

    with event_lock:
        previous = result_store.get(key)

    if result.coverage.status is CoverageStatus.FAILED and previous is not None:
        coverage = CoverageResult.merge([previous.coverage, result.coverage]).as_stale()
        cached = CachedResult(
            events=previous.events,
            coverage=coverage,
            version=previous.version,
            fetched_at=previous.fetched_at,
        )
    else:
        cached = CachedResult(
            events=result.events,
            coverage=result.coverage,
            version=CALENDAR_DATA_VERSION,
            fetched_at=now(),
        )

    update_result(key, cached)
    request_finished(key)


def events_async(
    key: str,
    url: str | None = None,
    file: str | Path | None = None,
    start: datetime | None = None,
    string_content: bytes | str | None = None,
    end: datetime | None = None,
    fix_apple: bool = False,
) -> None:
    """
    Trigger an asynchronous data request.

    :param key: key for data source to get result later
    :param url: iCal URL
    :param file: iCal file path
    :param string_content: iCal content as string
    :param start: start date
    :param end: end date
    :param fix_apple: fix known Apple iCal issues
    """
    t = Thread(
        target=request_data,
        args=(key, url, file, string_content, start, end, fix_apple),
    )

    with event_lock:
        if key not in threads:
            threads[key] = []

        threads[key].append(t)

        if not threads[key][0].is_alive():
            threads[key][0].start()


def request_finished(key: str) -> None:
    """
    Remove finished Thread from queue.

    :param key: data source key
    """
    with event_lock:
        if key not in threads or not threads[key]:
            return

        threads[key] = threads[key][1:]

        if threads[key]:
            threads[key][0].run()


def update_events(key: str, data: list[Event]) -> None:
    """
    Set the latest events for a key.

    :param key: key to set
    :param data: events for key
    """
    with event_lock:
        event_store[key] = data


def update_result(key: str, cached: CachedResult) -> None:
    """
    Set the latest cached result (events, coverage and version) for a key.

    :param key: key to set
    :param cached: cached result for key
    """
    with event_lock:
        event_store[key] = cached.events[:]
        result_store[key] = cached


def latest_events(key: str) -> list[Event]:
    """
    Get the latest downloaded events for the given key.

    :return: events for key
    """
    with event_lock:
        # copy data
        res = event_store[key][:]

    return res


def latest_result(key: str) -> CachedResult:
    """
    Get the latest cached result including coverage status and version.

    Entries produced with an older data version are reported as stale so the
    caller knows the cached events must not be treated as authoritative.

    :return: cached result for key
    """
    with event_lock:
        cached = result_store[key]

    if cached.version != CALENDAR_DATA_VERSION and not cached.coverage.stale:
        cached = CachedResult(
            events=cached.events,
            coverage=cached.coverage.as_stale(),
            version=cached.version,
            fetched_at=cached.fetched_at,
        )

    return cached


def all_done(key: str) -> bool:
    """
    Check if requests for the given key are active.

    :param key: key for requests
    :return: True if requests are pending or active
    """
    with event_lock:
        if threads[key]:
            return False
        return True
