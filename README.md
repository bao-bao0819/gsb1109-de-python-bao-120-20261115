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

# Contributing

You will need [poetry](https://github.com/python-poetry/poetry) and [pre-commit](https://pre-commit.com/index.html) installed and than run.

```bash
pre-commit install
```

Happy contributing!

### Incremental window updates

`diff_calendars` compares two snapshots and rebuilds only instances inside one
query window.  It separates recurrence-rule changes, timezone-definition
changes, single-occurrence exceptions and display-only edits:

```python
from datetime import date

from icalevents.icaldiff import diff_calendars

result = diff_calendars(
    previous_ics,
    current_ics,
    start=date(2024, 1, 1),
    end=date(2024, 1, 31),
)

result.added                      # new instances
result.removed                    # withdrawn instances
result.attribute_changes          # summary/location/... only, no remove+add
result.structural_changes         # time, rule, or timezone changes
result.cancellation_changes       # STATUS:CANCELLED with before/after events
result.series_changes             # RRULE/EXDATE/DTSTART-level changes
result.timezone_changes           # VTIMEZONE definition changes
result.apply(existing_events)     # apply the diff to a local window
```

An instance is identified by `(UID, original occurrence)`.  Moving an
exception keeps its identity because the original `RECURRENCE-ID` remains the
anchor.  Instances outside the window are never used as evidence for deleting
confirmed instances inside it; such rows are returned in `result.protected`.
