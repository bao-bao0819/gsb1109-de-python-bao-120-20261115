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

Incremental window updates
--------------------------

.. code:: python

  from datetime import date
  from icalevents.icaldiff import diff_calendars

  result = diff_calendars(old_ics, new_ics, date(2024, 1, 1), date(2024, 1, 31))

  result.added                 # instances to insert
  result.removed               # instances to withdraw
  result.changed               # in-place updates
  result.cancelled             # STATUS:CANCELLED changes (also in removed)
  result.series_changes        # RRULE / EXDATE / DTSTART changes
  result.timezone_changes      # VTIMEZONE definition changes
  result.protected             # unproven removals that were retained

Only instances overlapping the requested window are compared.  Identity is the
pair ``(UID, original occurrence)``; an exception moved to another day keeps
the same key because its original ``RECURRENCE-ID`` is retained.
