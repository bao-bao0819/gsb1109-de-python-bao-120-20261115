.. iCalEvents documentation master file, created by
   sphinx-quickstart on Wed Sep  8 21:48:51 2021.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Welcome to iCalEvents's documentation!
======================================

Simple Python 3 library to download, parse and query iCal sources.


Usage
=====

iCloud:
-------

.. code:: python

  from icalevents.icalevents import events

  es  = events(<iCloud URL>, fix_apple=True)

Google:
-------

.. code:: python

  from icalevents.icalevents import events

  es  = events(<Google Calendar URL>)


Business day coverage
---------------------

To count how many business days each activity actually covers, build the
venue's business windows from its open dates, opening/closing times and
timezone, and intersect them with the expanded events:

.. code:: python

  from datetime import date, time
  from icalevents.coverage import business_calendar, coverage_by_uid
  from icalevents.icalevents import events

  es = events(file="venue.ics", start=date(2024, 1, 1), end=date(2024, 2, 1))
  windows = business_calendar(
      business_days=[date(2024, 1, 1), date(2024, 1, 2)],
      open_time=time(9, 0),
      close_time=time(18, 0),
      tz="Europe/Berlin",
  )
  # UID -> number of distinct business days covered
  print(coverage_by_uid(es, windows))

A closing time at or before the opening time is treated as a window that runs
past midnight. A day is counted only when an event really overlaps that day's
window; overlapping instances of the same event (same UID) count the day once.
Cancelled events (``STATUS:CANCELLED``) never count and transparent events
(``TRANSP:TRANSPARENT``) are skipped unless ``include_transparent=True``.
The one-shot helper :func:`icalevents.coverage.event_coverage` takes the iCal
source directly and returns the same UID to day-count mapping.


Example
=======

see main.py

.. code:: python

   from icalevents.icalevents import events_async, latest_events, all_done
   from time import sleep

   if __name__ == '__main__':
      keys = []

      with open('calendars.txt', mode='r', encoding='utf-8') as f:
         counter = 1

         while True:
               line = f.readline()
               if not line:
                  break

               name, url = line.split(maxsplit=1)
               name = name.strip()
               url = url.strip()

               fix_apple = False
               if name == 'icloud':
                  fix_apple = True

               key = "req_%d" % counter
               counter += 1
               keys.append(key)
               events_async(key, url, fix_apple=fix_apple)

      while keys:
         print("%d request running." % len(keys))

         for k in keys[:]:
               if all_done(k):
                  print("Request %s finished." % k)
                  keys.remove(k)

                  es = latest_events(k)

                  for e in es:
                     print(e)

         sleep(2)


API
===

Module contents
---------------

.. automodule:: icalevents
   :members:
   :undoc-members:
   :show-inheritance:

Submodules
----------

icalevents.icaldownload module
------------------------------

.. automodule:: icalevents.icaldownload
   :members:
   :undoc-members:
   :show-inheritance:

icalevents.icalevents module
----------------------------

.. automodule:: icalevents.icalevents
   :members:
   :undoc-members:
   :show-inheritance:

icalevents.icalparser module
----------------------------

.. automodule:: icalevents.icalparser
   :members:
   :undoc-members:
   :show-inheritance:

icalevents.coverage module
--------------------------

.. automodule:: icalevents.coverage
   :members:
   :undoc-members:
   :show-inheritance:
