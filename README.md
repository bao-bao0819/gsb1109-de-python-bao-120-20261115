# iCalEvents

Simple Python 3 library to download, parse and query iCal sources.

[![PyPI version](https://badge.fury.io/py/icalevents.svg)](https://badge.fury.io/py/icalevents)[![Jazzband](https://jazzband.co/static/img/badge.svg)](https://jazzband.co/)

## Build info

last push: [![Run pytest](https://github.com/jazzband/icalevents/actions/workflows/tests.yml/badge.svg)](https://github.com/jazzband/icalevents/actions/workflows/tests.yml)

master: [![Run pytest](https://github.com/jazzband/icalevents/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/jazzband/icalevents/actions/workflows/tests.yml?query=branch%3Amaster++)

## Documentation

https://icalevents.readthedocs.io/en/latest/

## Usage

### iCloud:

```python

from icalevents.icalevents import events

es  = events(<iCloud URL>, fix_apple=True)
```

### Google:

```python

from icalevents.icalevents import events

es  = events(<Google Calendar URL>)
```

## Resource occupancy with preparation and cleanup buffers

A booking can reserve preparation time before it and cleanup time after it,
configured per resource. Recurring events are expanded into their actual
occurrences before occupancy is calculated; each resource and calendar day
reports the merged occupancy union, the original meeting time, and the buffer
increment.

```python
from datetime import date, timedelta

from icalevents.icalevents import events
from icalevents.occupancy import ResourceRule, daily_occupancy

bookings = events(file="bookings.ics", start=date(2024, 5, 1), end=date(2024, 5, 8))

report = daily_occupancy(
    bookings,
    rules={
        "Studio A": ResourceRule(
            preparation=timedelta(minutes=15), cleanup=timedelta(minutes=10)
        ),
        "Van 1": (timedelta(minutes=5), timedelta()),
    },
    start=date(2024, 5, 1),
    end=date(2024, 5, 8),
    resource_of="location",  # or a callable; defaults to RESOURCE/LOCATION
)

for row in report:
    print(row.resource, row.day, row.intervals)
    print("  meeting:", row.meeting_duration)
    print("  buffer increment:", row.buffer_increment)
```

`row.intervals` is the union of all buffered intervals clipped to that date, so
overlapping or adjacent buffers are counted once. `CANCELLED` occurrences are
ignored. A buffer that crosses midnight is split, and each part is accounted to
the calendar date on which it falls. `calendar_occupancy(...)` reads an
iCalendar source and performs the same calculation in one call.

# Contributing

You will need [poetry](https://github.com/python-poetry/poetry) and [pre-commit](https://pre-commit.com/index.html) installed and than run.

```bash
pre-commit install
```

Happy contributing!
