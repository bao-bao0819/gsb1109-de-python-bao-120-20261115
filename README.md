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

### Resource capacity and overload windows

A resource calendar declares how many units each booking occupies and how
supply changes during maintenance. `icalevents.capacity` reports the exact
intervals where demand exceeds supply, with the bookings responsible.

```python
from datetime import datetime
from icalevents.capacity import analyze_calendar

report = analyze_calendar(
    file="rack-room.ics",
    start=datetime(2026, 3, 1),
    end=datetime(2026, 3, 20),
)

for segment in report.overloads:
    print(segment.start, segment.end, segment.overload)
    for booking in segment.excess_sources:
        print("  ", booking.label, booking.quantity)
```

Declarations live on `VEVENT` components:

- `X-RESOURCE-QUANTITY`: units a booking occupies. Missing means unknown, never
  zero.
- `X-RESOURCE-CAPACITY`: absolute supply during the event (a supply window,
  not a booking).
- `X-RESOURCE-CAPACITY-DELTA`: units removed during maintenance (also a supply
  window).

Intervals are half-open, so a booking ending at 10:00 does not conflict with
one starting at 10:00. Recurring bookings are expanded to their real instances;
`EXDATE`, `RECURRENCE-ID` overrides and `STATUS:CANCELLED` are honoured.
`TRANSP:TRANSPARENT` bookings do not consume capacity. Duplicate rows for the
same instance are collapsed, preferring the highest `SEQUENCE`.

Segments are classified as `ok`, `overload`, or `unknown`. `unknown` is used
when demand exists but capacity is not declared (`capacity_undeclared`) or a
booking has no quantity (`booking_quantity_undeclared`). If the declared
quantities alone already exceed capacity, the segment is a real `overload`
and the unknown-quantity bookings are listed alongside it.

The command line entry point prints the same analysis:

```bash
ical-capacity rack-room.ics --start 2026-03-01 --end 2026-03-20
ical-capacity rack-room.ics --capacity 8 --json
ical-capacity rack-room.ics --maintenance 2026-03-02T10:45,2026-03-02T11:15,3
```

# Contributing

You will need [poetry](https://github.com/python-poetry/poetry) and [pre-commit](https://pre-commit.com/index.html) installed and than run.

```bash
pre-commit install
```

Happy contributing!
