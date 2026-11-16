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

## Message ID normalization and receipts

Gateways may return the same message identifier in different representations:
a decimal number in `submit_sm_resp`, a hexadecimal `receipted_message_id`
TLV in a delivery receipt, and a plain textual identifier in the receipt
text. `smpplib.message_ids` normalizes those representations using explicit,
versioned rules and correlates submit responses with delivery receipts.

```python
from smpplib import message_ids

rules = message_ids.MessageIdRules({
    'versions': {
        '1.0': {'receipted_message_id': 'hex'},
        '2.0': {'receipted_message_id': 'hex'},
    },
})
client = smpplib.client.Client(
    'example.com', SOMEPORTNUMBER,
    message_id_version='2.0',
    message_id_gateway='gateway-a',
    message_id_rules=rules,
)

result = client.correlate_message_ids(submit_responses, receipts)
for link in result.linked:
    print(link.message_id, link.raw_submit, link.raw_receipt)
for unmatched in result.unmatched:
    print(unmatched.raw, unmatched.field, unmatched.reason)
```

`gateway-a` is recorded on each normalized value; gateway-specific overrides use
`MessageIdRules({'gateways': {'gateway-a': {'receipt_text.id': 'text'}}})`.

Rules apply only when they declare a format. An all-digit value is treated as
decimal, a textual value stays text, and a hex-looking string is **not**
converted unless its field rule or an explicit `0x` / `hex:` / `decimal:`
prefix declares it as hexadecimal. Values that cannot be interpreted under
their rule are listed in `result.unmatched` instead of being coerced. Every
`NormalizedMessageId` keeps the untouched original in `.raw`, so it can be
stored separately from the normalized value.

