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

## Billing reconciliation

`smpplib.billing` reconciles traffic against vendor bills using evidence only.
It keeps three independent calibres side by side: the **business notification
count** (one per message), the **encoding segment count** (parts produced by
`make_parts`, counted once regardless of retries) and the **gateway confirmed
charge count** (segments backed by an explicit vendor charge receipt).

A successful `submit_sm_resp` only proves acceptance. Without a
`ChargeReceipt` the amount stays `None` and is never guessed; a delivery
receipt proves handset outcome, not payment. When a long SMS partially fails,
each segment is booked on its own evidence instead of marking the whole
message as success or zero.

```python
from smpplib import consts, gsm
from smpplib.billing import (
    BillingLedger, MessageBilling, SubmitAttempt,
    DeliveryReceipt, ChargeReceipt,
)

parts, encoding, _ = gsm.make_parts(u'long text ...')
ledger = BillingLedger(currency='CNY')
ledger.add_message(MessageBilling.from_parts('order-1', parts, encoding))

# Every submit attempt (including retries) is kept as evidence.
ledger.record_attempt('order-1', SubmitAttempt(
    sequence=101, part_number=1, attempt_number=1,
    status=consts.SMPP_ESME_ROK, message_id='gw-1'))

# Gateway delivery receipt and the vendor bill line for that message_id.
ledger.attach_delivery_receipt(DeliveryReceipt('gw-1', stat='DELIVRD'))
ledger.attach_charge_receipt(
    ChargeReceipt('gw-1', amount='0.045', currency='CNY', bill_id='BILL-1'))

print(ledger.render())
print(ledger.totals())
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
