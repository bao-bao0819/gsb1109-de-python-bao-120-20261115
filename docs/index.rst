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

Resource capacity:
------------------

Bookings may declare ``X-RESOURCE-QUANTITY``; supply events may declare
``X-RESOURCE-CAPACITY`` or ``X-RESOURCE-CAPACITY-DELTA``.  The capacity
module reports the exact half-open intervals where demand exceeds supply and
the bookings that overlap them.

.. code:: python

  from datetime import datetime
  from icalevents.capacity import analyze_calendar

  report = analyze_calendar(
      file="rack-room.ics",
      start=datetime(2026, 3, 1),
      end=datetime(2026, 3, 20),
  )
  for segment in report.overloads:
      print(segment.start, segment.end, segment.overload)

Segments with missing declarations are returned as ``unknown`` rather than
being counted as zero.  From the command line:

.. code:: bash

  ical-capacity rack-room.ics --start 2026-03-01 --end 2026-03-20 --json


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

icalevents.capacity module
--------------------------

.. automodule:: icalevents.capacity
   :members:
   :undoc-members:
   :show-inheritance:
