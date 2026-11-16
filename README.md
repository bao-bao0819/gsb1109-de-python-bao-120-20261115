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

### Send plan and segment ranges

`make_send_plan()` returns the complete plan without sending anything. It
chooses the encoding from the text (falling back to UCS-2), counts each GSM
extension character as two units, keeps UTF-16 surrogate pairs together, and
recalculates capacity with the actual encoding:

```python
plan = smpplib.gsm.make_send_plan(u'Hello ^ world' * 30)

plan.encoding          # data_coding value to use
plan.esm_class         # UDH flag for a concatenated message
plan.parts             # bytes for short_message, including the UDH
plan.total_segments

for segment in plan:
    segment.character_range   # half-open (start, end) in the original text
    segment.text              # original text covered by this segment
    segment.body              # encoded payload without the UDH
    segment.udh               # 05 00 03 <ref> <total> <seq>, or b''
```

Consecutive segments form a complete, non-overlapping partition of the
original text. A concatenated UDH always uses the six-byte form
`05 00 03 <reference> <total> <sequence>`; a single message has no UDH.

If a character cannot be represented, nothing is replaced and no plan is
returned. `smpplib.exceptions.UnencodableCharacters` reports the zero-based
positions in the original string:

```python
try:
    smpplib.gsm.make_send_plan(u'bad \ud800 text')
except smpplib.exceptions.UnencodableCharacters as error:
    error.positions   # [4]
    error.characters  # ('\ud800',)
```

`make_parts()` keeps its existing three-item return value
`(parts, encoding, esm_class)`; the same metadata is also available on the
returned value as `.plan` and `.character_ranges`.
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
