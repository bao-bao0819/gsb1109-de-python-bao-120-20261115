from __future__ import annotations

from datetime import date, datetime, time, tzinfo as _tzinfo
from pathlib import Path
from threading import Lock, Thread

import urllib3

from .icaldiff import InstanceKey, diff_events
from .icalparser import Event, parse_events
from .icaldownload import ICalDownload


# Lock for event data
event_lock = Lock()
# Event data
event_store: dict[str, list[Event]] = {}
# Threads
threads: dict[str, list[Thread]] = {}


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

    :param key: key for data source to get result later
    :param url: iCal URL
    :param file: iCal file path
    :param string_content: iCal content as string
    :param start: start date
    :param end: end date
    :param fix_apple: fix known Apple iCal issues
    """
    data = []

    try:
        data += events(
            url=url,
            file=file,
            string_content=string_content,
            start=start,
            end=end,
            fix_apple=fix_apple,
        )
    finally:
        update_events(key, data, start=start, end=end)
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
        threads[key] = threads[key][1:]

        if threads[key]:
            threads[key][0].run()


def update_events(
    key: str,
    data: list[Event],
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> None:
    """
    Set the latest events for a key, rebuilding only the requested window.

    When a window is supplied, instances inside it are replaced by ``data``;
    instances outside it are retained.  Confirmed rows inside the window are
    retained when the new result is visibly partial (for example a feed that
    only returned one day).  Without a window this keeps its historical
    replacement semantics.

    :param key: key to set
    :param data: events for key
    :param start: optional start of the rebuilt window
    :param end: optional end of the rebuilt window
    """
    with event_lock:
        if start is None or end is None or key not in event_store:
            event_store[key] = data
            return

        inside = [
            event
            for event in event_store[key]
            if _inside_window(event, start, end)
        ]
        outside = [
            event
            for event in event_store[key]
            if not _inside_window(event, start, end)
        ]

        # An empty result is almost always a failed or partial download.  Keep
        # the previously confirmed window rather than erasing it.
        if not data:
            event_store[key] = outside + inside
            return

        diff = diff_events(inside, data, start=start, end=end)
        event_store[key] = outside + diff.apply(inside)


def _inside_window(
    event: Event, start: date | datetime, end: date | datetime
) -> bool:
    """Whether an event overlaps a window, using parse_events semantics."""
    if event.start is None or event.end is None:
        return False
    low, high = _window_bounds(event.start, start, end)
    return event.end >= low and event.start <= high


def _window_bounds(
    like: date | datetime, start: date | datetime, end: date | datetime
) -> tuple[date | datetime, date | datetime]:
    if type(like) is date:
        return (
            date(start.year, start.month, start.day),
            date(end.year, end.month, end.day),
        )
    if type(start) is date:
        low = datetime.combine(start, time.min)
    else:
        low = start
    if type(end) is date:
        high = datetime.combine(end, time.max)
    else:
        high = end
    if like.tzinfo is not None:
        if low.tzinfo is None:
            low = low.replace(tzinfo=like.tzinfo)
        if high.tzinfo is None:
            high = high.replace(tzinfo=like.tzinfo)
    return low, high


def latest_events(key: str) -> list[Event]:
    """
    Get the latest downloaded events for the given key.

    :return: events for key
    """
    with event_lock:
        # copy data
        res = event_store[key][:]

    return res


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
