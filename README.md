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

### Public subscription export:

A public subscription must only show resource occupancy. Pass `public=True` to
redact summaries, descriptions, locations, attendees, organizers, categories and
URLs while keeping the time range, the busy/free state and the relation between
instances of a recurring event:

```python

from icalevents.icalevents import events

es = events(<iCal URL>, public=True)
for e in es:
    print(e.start, e.end, e.summary, e.occupies_resource())
```

Transparent and cancelled events are kept but do not occupy the resource.
Private and confidential events are still exported as occupancy entries; they
are not dropped. To adjust the policy:

```python

from icalevents.public import PublicExportPolicy, export_feed, public_events

policy = PublicExportPolicy(anonymize_uid=True)
es = events(<iCal URL>, policy=policy)

# Redact a raw feed while keeping RRULE, EXDATE and RECURRENCE-ID.
redacted = export_feed(open("calendar.ics", "rb").read(), policy)
```

# Contributing

You will need [poetry](https://github.com/python-poetry/poetry) and [pre-commit](https://pre-commit.com/index.html) installed and than run.

```bash
pre-commit install
```

Happy contributing!
