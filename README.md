`python-smpplib`
================

[![Version](https://img.shields.io/pypi/v/smpplib.svg?style=flat)](https://pypi.org/project/smpplib/#history)
[![Python versions](https://img.shields.io/pypi/pyversions/smpplib.svg?style=flat)](https://pypi.org/project/smpplib/)
[![PyPI downloads](https://img.shields.io/pypi/dm/smpplib.svg?style=flat)](https://pypi.org/project/smpplib/#files)
![License](https://img.shields.io/pypi/l/smpplib.svg?style=flat)
[![CircleCI](https://circleci.com/gh/python-smpplib/python-smpplib.svg?style=svg)](https://circleci.com/gh/python-smpplib/python-smpplib)

SMPP library for Python. Forked from [Google Code](https://code.google.com/p/smpplib/).

Example:

```python
import logging
import sys

import smpplib.gsm
import smpplib.client
import smpplib.consts

# if you want to know what's happening
logging.basicConfig(level='DEBUG')

# Two parts, UCS2, SMS with UDH
parts, encoding_flag, msg_type_flag = smpplib.gsm.make_parts(u'Привет мир!\n'*10)

client = smpplib.client.Client('example.com', SOMEPORTNUMBER, allow_unknown_opt_params=True)

# Print when obtain message_id
client.set_message_sent_handler(
    lambda pdu: sys.stdout.write('sent {} {}\n'.format(pdu.sequence, pdu.message_id)))
client.set_message_received_handler(
    lambda pdu: sys.stdout.write('delivered {}\n'.format(pdu.receipted_message_id)))

client.connect()
client.bind_transceiver(system_id='login', password='secret')

for part in parts:
    pdu = client.send_message(
        source_addr_ton=smpplib.consts.SMPP_TON_INTL,
        #source_addr_npi=smpplib.consts.SMPP_NPI_ISDN,
        # Make sure it is a byte string, not unicode:
        source_addr='SENDERPHONENUM',

        dest_addr_ton=smpplib.consts.SMPP_TON_INTL,
        #dest_addr_npi=smpplib.consts.SMPP_NPI_ISDN,
        # Make sure thease two params are byte strings, not unicode:
        destination_addr='PHONENUMBER',
        short_message=part,

        data_coding=encoding_flag,
        esm_class=msg_type_flag,
        registered_delivery=True,
    )
    print(pdu.sequence)
    
# Enters a loop, waiting for incoming PDUs
client.listen()
```
You also may want to listen in a thread:
```python
from threading import Thread
t = Thread(target=client.listen)
t.start()
```
**Note:** When listening, the client will automatically send an `enquire_link` command when the socket timeouts. You may override that behavior by passing `auto_send_enquire_link=False` as an argument to `listen()`. In that case, `socket.timeout` exceptions will bubble up.

The client supports setting a custom generator that produces sequence numbers for the PDU packages. Per default a simple in memory generator is used which in conclusion is reset on (re)instantiation of the client, e.g. by an application restart. If you want to keep the sequence number to be persisted across restarts you can implement your own storage backed generator.

Example:

```python
import smpplib.client

import mymodule

generator = mymodule.PersistentSequenceGenerator()
client = smpplib.client.Client('example.com', SOMEPORTNUMBER, sequence_generator=generator)
...
```

Offline dispatch plan verification
-----------------------------------

`smpplib.retry` verifies notification dispatch plans without any network IO.
Feed it a timestamped event log (enqueue, submit attempts and their outcomes,
reconciliation decisions) together with the backoff, send-window and
rate-limit rules; it replays the log on a simulation clock and returns the
decision timeline plus any policy violations.

```python
from smpplib import consts
from smpplib.retry import (
    BusinessIdentity, Event, BackoffPolicy, SendWindow, Window,
    RateLimiter, RetryPlanner, simulate,
)

identity = BusinessIdentity('notification-uuid-123')

planner = RetryPlanner(
    backoff=BackoffPolicy(initial_delay=2, multiplier=2, max_delay=60,
                          max_attempts=5),
    send_window=SendWindow((Window(0, 24 * 3600),)),
    rate_limiter=RateLimiter(limit=10, interval=1),
)

events = [
    Event.enqueue(0, identity),
    # Explicit throttling: retryable, the business identity stays the same.
    Event.attempt(1, identity, status=consts.SMPP_ESME_RTHROTTLED, sequence=1),
    # Connection broke before any response: the outcome is unknown, not a
    # rejection and not permission to resend.
    Event.attempt(3, identity, interrupted=True, sequence=2),
    # Only an explicit reconciliation that the SMSC did not accept it unlocks
    # a resend.
    Event.reconcile(10, identity, accepted=False),
    Event.attempt(14, identity, status=consts.SMPP_ESME_ROK, sequence=3,
                  reference='notification-uuid-123'),
]

result = simulate(events, planner)
result.ok            # False if any rule was violated
result.timeline      # dispatch / retry / hold / succeeded decisions
result.violations    # e.g. an unsafe resend while the outcome was unknown
```

Outcome rules:

- An explicit `SMPP_ESME_ROK` accepts the message; an explicit non-retryable
  status is a definite rejection and is never retried.
- Throttling / queue-full / temporary errors are retryable. Retries keep the
  same `BusinessIdentity`; only the SMPP sequence number changes.
- A timeout or disconnect with no response means the result is **unknown**.
  The job is held for reconciliation (`query_sm` or operator confirmation)
  and must not be resent automatically — the SMSC may already have accepted
  it. Replaying a log that resends anyway reports an `unsafe_resend_after_unknown_outcome`
  violation.

