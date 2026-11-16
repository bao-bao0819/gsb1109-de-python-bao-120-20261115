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

Reassembling concatenated messages
----------------------------------

`smpplib.reassembly` joins the parts of a long message received from an SMSC
or read from an offline PDU archive. It accepts parsed PDU objects, dicts with
SMPP field names, and raw short-message payloads. Parts are grouped by source,
destination and fragment evidence:

* concatenation UDH with an 8-bit reference (`0x00`);
* concatenation UDH with a 16-bit reference (`0x08`);
* `sar_msg_ref_num` / `sar_total_segments` / `sar_segment_seqnum`;
* explicit archive fields such as `reference`, `total` and `sequence`.

A reference number may be reused after a message is complete, and every
physical receipt is kept as an occurrence. Missing parts keep their
position with a gap marker; duplicate parts are exposed through
`duplicates`/`duplicate_sequences`, and contradictory payloads at the same
position are exposed through `conflicts` instead of overwriting the first
part.

```python
import smpplib.reassembly

messages = smpplib.reassembly.reassemble(received_pdus)
for message in messages:
    print(message.source, message.destination, message.complete, message.body)
```

The full byte sequence of a complete message is joined before decoding, so a
UTF-16 character (including a surrogate pair) split across two parts decodes
correctly.

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
