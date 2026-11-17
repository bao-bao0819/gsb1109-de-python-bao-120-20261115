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

### Meeting attendance and quorum

```python
from icalevents.attendance import AliasTable, AttendancePolicy, analyze_calendar

reports = analyze_calendar(
    file="meeting.ics",
    aliases=AliasTable({
        "alice@example.com": ["a.smith@example.com", "Alice Smith"],
    }),
    policy=AttendancePolicy.from_mapping({"CHAIR": 1, "REQ-PARTICIPANT": 3}),
)

for report in reports:
    print(report.summary, report.headcount, report.all_requirements_met)
```

`analyze_event` works on one parsed `Event`; `analyze_calendar` reads a
calendar source and analyzes every occurrence in the requested range.

- `confirmed` contains only `PARTSTAT=ACCEPTED`. `pending` contains
  `NEEDS-ACTION` and attendees with no `PARTSTAT`; `declined` and `tentative`
  are separate, so a non-reply is never treated as either answer.
- `headcount` is a `CountRange(minimum, maximum)`: confirmed people at the
  low end, plus pending, tentative, and conflicting replies at the high end.
  Declined people are excluded from both ends.
- Duplicate invitations are collapsed case-insensitively by e-mail address.
  Different addresses or names are collapsed only when declared in an
  `AliasTable`; nothing is guessed from similar names.
- `DELEGATED-TO`/`DELEGATED-FROM` replaces the delegator's seat with the
  delegate instead of counting two people. Chains are listed in
  `delegation_chains`, cycles in `delegation_cycles`; cyclic invitations are
  excluded from the headcount.
- `requirements` checks each role's minimum and reports `satisfied`,
  `at_risk` (only possible if outstanding replies accept), or `violated`.
  Rooms, resources, and informational `NON-PARTICIPANT`s do not count by
  default.

# Contributing

You will need [poetry](https://github.com/python-poetry/poetry) and [pre-commit](https://pre-commit.com/index.html) installed and than run.

```bash
pre-commit install
```

Happy contributing!
