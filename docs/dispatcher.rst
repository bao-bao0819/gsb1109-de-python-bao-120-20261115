Dispatcher
============

The dispatcher maps OSC addresses to functions and calls the functions with the messages' arguments.
Function can also be mapped to wildcard addresses.


Example
---------

.. code-block:: python

    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_message_builder import ArgValue

    dispatcher = Dispatcher()


    def set_filter(address: str, *args: ArgValue) -> None:
        # We expect two float arguments
        if not len(args) == 2 or type(args[0]) is not float or type(args[1]) is not float:
            return

        # Check that address starts with filter
        if not address[:-1] == "/filter":  # Cut off the last character
            return

        value1 = args[0]
        value2 = args[1]
        filterno = address[-1]
        print(f"Setting filter {filterno} values: {value1}, {value2}")


    dispatcher.map("/filter*", set_filter)  # Map wildcard address to set_filter function

    # Set up server and client for testing
    from pythonosc.osc_server import BlockingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient

    server = BlockingOSCUDPServer(("127.0.0.1", 1337), dispatcher)
    client = SimpleUDPClient("127.0.0.1", 1337)

    # Send message and receive exactly one message (blocking)
    client.send_message("/filter1", [1., 2.])
    server.handle_request()

    client.send_message("/filter8", [6., -2.])
    server.handle_request()


Mapping
---------

The dispatcher associates addresses with functions by storing them in a mapping.
An address can contains wildcards as defined in the OSC specifications.
Call the ``Dispatcher.map`` method with an address pattern and a handler callback function:

.. code-block:: python

    from pythonosc.dispatcher import Dispatcher
    disp = Dispatcher()
    disp.map("/some/address*", some_printing_func)

This will for example print any OSC messages starting with ``/some/address``.

Additionally you can provide any amount of extra fixed argument that will always be passed before the OSC message arguments:

.. code-block:: python

    handler = disp.map("/some/other/address", some_printing_func, "This is a fixed arg", "and this is another fixed arg")

The handler callback signature must look like this (choose the form matching
whether fixed arguments were supplied):

.. code-block:: python

    from typing import Any, List
    from pythonosc.osc_message_builder import ArgValue

    def some_callback(address: str, *osc_arguments: ArgValue) -> None: ...
    def some_callback(address: str, fixed_argument: List[Any], *osc_arguments: ArgValue) -> None: ...

The annotation on ``*osc_arguments`` describes each received OSC argument;
inside the callback, ``osc_arguments`` is a tuple of those values. ``ArgValue``
includes scalar values, blobs, MIDI packets and OSC arrays. Fixed arguments
passed to ``map`` are instead collected into one separate ``List[Any]``.
You can also declare a fixed number of positional parameters when you know
which OSC arguments the address accepts.

The ``Dispatcher.map`` method returns a ``Handler`` object, which can be used to remove the mapping from the dispatcher.


Unmapping
-----------

A mapping can be undone with the ``Dispatcher.unmap`` method, which takes an address and ``Handler`` object as arguments.
For example, to unmap the mapping from the `Mapping`_ section:

.. code-block:: python

    disp.unmap("some/other/address", handler)

Alternatively the handler can be reconstructed from a function and optional fixed argument:

.. code-block:: python

    disp.unmap("some/other/address", some_printing_func, *some_fixed_args)

If the provided mapping doesn't exist, a ``ValueError`` is raised.


Default Handler
-----------------

It is possible to specify a handler callback function that is called for every unmatched address:

.. code-block:: python

    disp.set_default_handler(some_handler_function)

This is extremely useful if you quickly need to find out what addresses an undocumented device is transmitting on or for building a learning function for some controls.
The handler must have the same signature as map callbacks:

.. code-block:: python

    def some_callback(address: str, *osc_arguments: ArgValue) -> None: ...


Handler Responses
-----------------

Handler functions can return responses back to the client, when running on a server, or to the
server when running as a client.  Handler functions should return one of:

* None
* An OSC address in string format
* A tuple containing a string OSC address and the associated arguments

If the handler function response is not None it will be encoded in an OSCMessage and sent to the
remote client or server.


Configuration Versioning
-------------------------

Subscriptions can be added or removed while the server is running. To keep processing predictable,
each incoming message is pinned to one immutable configuration version before any of its handlers
run. The handler set matched for that message therefore stays fixed for its whole lifetime:

* Remapping or unmapping an address from inside a handler does not change the handlers of the
  message currently being processed.
* Such a change only affects subsequent messages (including later messages inside the same bundle),
  which capture a fresh configuration snapshot.

The current configuration version is exposed as the read-only ``Dispatcher.version`` attribute and
is incremented on every ``map``, ``unmap`` or ``set_default_handler`` call.

A single message may still match several subscriptions. Each matched ``Handler`` instance is
invoked at most once per message, even when it is reachable through several matching addresses, so
the same subscription is never dispatched twice for one message.


Handler Failures
-----------------

A failing callback must not prevent the other subscriptions matched by the same message from
running. When a handler raises, the exception is caught, logged, and recorded as a
``DispatchFailure`` rather than propagated. The remaining matched handlers are still invoked, and
each raised subscription produces its own record.

Failures are available from the read-only ``Dispatcher.failures`` list and can be reset with
``Dispatcher.clear_failures``. Pass ``failure_buffer_size`` to the constructor to cap how many
records are retained; when the buffer is full the oldest records are dropped.

.. code-block:: python

    disp = Dispatcher(failure_buffer_size=100)
    disp.map("/some/address", some_callback)

    # ... after dispatching messages ...
    for failure in disp.failures:
        print(failure.address, failure.handler, failure.error)
    disp.clear_failures()


Dispatcher Module Documentation
---------------------------------

.. automodule:: pythonosc.dispatcher
  :special-members:
  :members:
  :exclude-members: __weakref__
