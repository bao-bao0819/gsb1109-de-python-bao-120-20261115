"""Maps OSC addresses to handler functions"""

import asyncio
import collections
import inspect
import logging
import re
import threading
import time
from dataclasses import dataclass
from pythonosc import osc_packet
from typing import (
    overload,
    List,
    Mapping,
    Union,
    Any,
    AnyStr,
    Generator,
    Tuple,
    Callable,
    Optional,
    DefaultDict,
)
from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import ArgValue


# Point-in-time, immutable view of the dispatcher configuration:
# (version, address -> handlers, default handler).
ConfigSnapshot = Tuple[int, "Mapping[str, Tuple[Handler, ...]]", Optional["Handler"]]


@dataclass
class DispatchFailure:
    """Record of a handler callback that raised while dispatching a message.

    A failure is captured instead of being propagated so that one broken
    callback cannot prevent the other handlers matched by the same message
    from running. Each raised callback produces its own record.
    """

    address: str
    handler: "Handler"
    client_address: Tuple[str, int]
    error: BaseException


class Handler(object):
    """Wrapper for a callback function that will be called when an OSC message is sent to the right address.

    Represents a handler callback function that will be called whenever an OSC message is sent to the address this
    handler is mapped to. It passes the address, the fixed arguments (if any) as well as all osc arguments from the
    message if any were passed.
    """

    def __init__(
        self,
        _callback: Callable,
        _args: Union[Any, List[Any]],
        _needs_reply_address: bool = False,
    ) -> None:
        """
        Args:
            _callback Function that is called when handler is invoked
            _args: Message causing invocation
            _needs_reply_address Whether the client's ip address shall be passed as an argument or not
        """
        self.callback = _callback
        self.args = _args
        self.needs_reply_address = _needs_reply_address

    # needed for test module
    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(self, type(other))
            and self.callback == other.callback
            and self.args == other.args
            and self.needs_reply_address == other.needs_reply_address
        )

    def invoke(
        self, client_address: Tuple[str, int], message: OscMessage
    ) -> Union[None, AnyStr, Tuple[AnyStr, ArgValue]]:
        """Invokes the associated callback function

        Args:
            client_address: Address match that causes the invocation
            message: Message causing invocation
        Returns:
            The result of the handler function can be None, a string OSC address, or a tuple of the OSC address
            and arguments.
        """
        if self.needs_reply_address:
            if self.args:
                return self.callback(
                    client_address, message.address, self.args, *message
                )
            else:
                return self.callback(client_address, message.address, *message)
        else:
            if self.args:
                return self.callback(message.address, self.args, *message)
            else:
                return self.callback(message.address, *message)

    async def async_invoke(
        self, client_address: Tuple[str, int], message: OscMessage
    ) -> Union[None, AnyStr, Tuple[AnyStr, ArgValue]]:
        """Invokes the associated callback function (asynchronously)

        Args:
            client_address: Address match that causes the invocation
            message: Message causing invocation
        Returns:
            The result of the handler function can be None, a string OSC address, or a tuple of the OSC address
            and arguments.
        """
        cb = self.callback
        is_async = inspect.iscoroutinefunction(cb)

        if self.needs_reply_address:
            if self.args:
                if is_async:
                    return await cb(
                        client_address, message.address, self.args, *message
                    )
                else:
                    return cb(client_address, message.address, self.args, *message)
            else:
                if is_async:
                    return await cb(client_address, message.address, *message)
                else:
                    return cb(client_address, message.address, *message)
        else:
            if self.args:
                if is_async:
                    return await cb(message.address, self.args, *message)
                else:
                    return cb(message.address, self.args, *message)
            else:
                if is_async:
                    return await cb(message.address, *message)
                else:
                    return cb(message.address, *message)


class Dispatcher(object):
    """Maps Handlers to OSC addresses and dispatches messages to the handler on matched addresses

    Maps OSC addresses to handler functions and invokes the correct handler when a message comes in.
    """

    def __init__(
        self,
        strict_timing: bool = True,
        failure_buffer_size: Optional[int] = None,
    ) -> None:
        """Initialize the dispatcher.

        Args:
            strict_timing: Whether to automatically schedule messages with future timetags.
                If True (default), the dispatcher will wait (using sleep) until the specified
                timetag before invoking handlers.
                If False, messages are dispatched immediately regardless of their timetag.
                Disabling this can prevent memory/thread accumulation issues when receiving
                many future-dated messages.
            failure_buffer_size: Maximum number of :class:`DispatchFailure` records to retain.
                When full, the oldest records are dropped. ``None`` (default) keeps every
                failure until :meth:`clear_failures` is called.
        """
        self._map: DefaultDict[str, List[Handler]] = collections.defaultdict(list)
        self._default_handler: Optional[Handler] = None
        self._strict_timing = strict_timing
        # Monotonic configuration version. Every mapping change (map/unmap/default
        # handler) bumps it, allowing in-flight messages to keep using a fixed
        # snapshot of the configuration they were matched against. The same lock
        # guards the mapping mutations and snapshot creation so that a message
        # never observes a half-applied configuration update.
        self._version = 0
        self._config_lock = threading.RLock()
        # One record per failed callback. Failures never interrupt the dispatch of
        # the other handlers matched by the same message.
        self._failures: collections.deque[DispatchFailure] = collections.deque(
            maxlen=failure_buffer_size
        )

    @property
    def failures(self) -> List[DispatchFailure]:
        """Return a snapshot copy of every recorded callback failure."""
        return list(self._failures)

    @property
    def version(self) -> int:
        """Monotonic configuration version, bumped on every mapping change."""
        return self._version

    def clear_failures(self) -> None:
        """Drop all recorded callback failures."""
        self._failures.clear()

    def _record_failure(
        self,
        address: str,
        handler: "Handler",
        client_address: Tuple[str, int],
        error: BaseException,
    ) -> None:
        """Store and log a single failed handler invocation without raising."""
        self._failures.append(
            DispatchFailure(
                address=address,
                handler=handler,
                client_address=client_address,
                error=error,
            )
        )
        logging.error(
            "Handler %r raised while dispatching %s",
            getattr(handler, "callback", handler),
            address,
            exc_info=error,
        )

    def _bump_version(self) -> None:
        """Advance the configuration version after a mapping change.

        Already-running dispatches hold their own snapshot and are unaffected;
        only subsequently processed messages observe the new version.
        """
        with self._config_lock:
            self._version += 1

    def _snapshot(self) -> ConfigSnapshot:
        """Return an immutable point-in-time view of the current configuration.

        The returned handler tuples are independent copies, so concurrent map or
        unmap calls cannot mutate what an in-flight message is iterating over.
        """
        with self._config_lock:
            return (
                self._version,
                {addr: tuple(handlers) for addr, handlers in self._map.items()},
                self._default_handler,
            )

    def map(
        self,
        address: str,
        handler: Callable,
        *args: Union[Any, List[Any]],
        needs_reply_address: bool = False,
    ) -> Handler:
        """Map an address to a handler

        The callback function must have one of the following signatures:

        .. code-block:: python

            def some_cb(address: str, *osc_args: ArgValue) -> Union[None, str, Tuple[str, ArgValue]]: ...
            def some_cb(address: str, fixed_args: List[Any], *osc_args: ArgValue) -> Union[None, str, Tuple[str, ArgValue]]: ...
            def some_cb(client_address: Tuple[str, int], address: str, *osc_args: ArgValue) -> Union[None, str, Tuple[str, ArgValue]]: ...
            def some_cb(client_address: Tuple[str, int], address: str, fixed_args: List[Any], *osc_args: ArgValue) -> Union[None, str, Tuple[str, ArgValue]]: ...

        Each variadic OSC argument is a single ArgValue, not a list of all arguments.
        Any fixed arguments passed to map() are collected into the separate fixed_args list.

        The callback function can return None, or a string representing an OSC address to be returned to the client,
        or a tuple that includes the address and ArgValue which will be converted to an OSC message and returned to
        the client.

        Args:
            address: Address to be mapped
            handler: Callback function that will be called as the handler for the given address
            *args: Fixed arguements that will be passed to the callback function
            needs_reply_address: Whether the IP address from which the message originated from shall be passed as
                an argument to the handler callback

        Returns:
            The handler object that will be invoked should the given address match

        """
        # TODO: Check the spec:
        # http://opensoundcontrol.org/spec-1_0
        # regarding multiple mappings
        handlerobj = Handler(handler, list(args), needs_reply_address)
        with self._config_lock:
            self._map[address].append(handlerobj)
            self._bump_version()
        return handlerobj

    @overload
    def unmap(self, address: str, handler: Handler) -> None:
        """Remove an already mapped handler from an address

        Args:
            address (str): Address to be unmapped
            handler (Handler): A Handler object as returned from map().
        """
        pass

    @overload
    def unmap(
        self,
        address: str,
        handler: Callable,
        *args: Union[Any, List[Any]],
        needs_reply_address: bool = False,
    ) -> None:
        """Remove an already mapped handler from an address

        Args:
            address: Address to be unmapped
            handler: A function that will be run when the address matches with
                the OscMessage passed as parameter.
            args: Any additional arguments that will be always passed to the
                handlers after the osc messages arguments if any.
            needs_reply_address: True if the handler function needs the
                originating client address passed (as the first argument).
        """
        pass

    def unmap(self, address, handler, *args, needs_reply_address=False):
        with self._config_lock:
            try:
                if isinstance(handler, Handler):
                    self._map[address].remove(handler)
                else:
                    self._map[address].remove(
                        Handler(handler, list(args), needs_reply_address)
                    )
            except ValueError as e:
                if str(e) == "list.remove(x): x not in list":
                    raise ValueError(
                        f"Address '{address}' doesn't have handler '{handler}' mapped to it"
                    ) from e
            self._bump_version()

    @staticmethod
    def _compile_address_pattern(
        address_pattern: str,
    ) -> Optional["re.Pattern[str]"]:
        """Convert an OSC Address Pattern to a compiled Python regular expression.

        Spec: https://opensoundcontrol.stanford.edu/spec-1_0.html#osc-address-patterns

        Returns ``None`` when the pattern cannot be compiled, in which case it
        matches nothing.
        """
        pattern = "^"
        i = 0
        while i < len(address_pattern):
            c = address_pattern[i]
            if c == "*":
                pattern += "[^/]*"
            elif c == "?":
                pattern += "[^/]"
            elif c == "[":
                pattern += "["
                i += 1
                if i < len(address_pattern) and address_pattern[i] == "!":
                    pattern += "^"
                    i += 1
                while i < len(address_pattern) and address_pattern[i] != "]":
                    if address_pattern[i] in r"\^$.|()+*?":
                        pattern += "\\"
                    pattern += address_pattern[i]
                    i += 1
                pattern += "]"
            elif c == "{":
                pattern += "("
                i += 1
                while i < len(address_pattern) and address_pattern[i] != "}":
                    char = address_pattern[i]
                    if char == ",":
                        pattern += "|"
                    elif char in r"\^$.|()[]+*?":
                        pattern += "\\" + char
                    else:
                        pattern += char
                    i += 1
                pattern += ")"
            elif c in r"\^$.|()[]+?":
                pattern += "\\" + c
            else:
                pattern += c
            i += 1
        pattern += "$"

        try:
            return re.compile(pattern)
        except re.error:
            # If the pattern is invalid, it won't match anything.
            return None

    def _matched_handlers(
        self,
        address_pattern: str,
        snapshot: ConfigSnapshot,
    ) -> List[Handler]:
        """Return every handler matching an address within a fixed configuration.

        The same :class:`Handler` instance is returned at most once even when it
        is reachable through several matching addresses, so a subscription is
        never dispatched twice for a single message.

        Args:
            address_pattern: Address to match
            snapshot: Configuration snapshot captured for this message

        Returns:
            Deduplicated list of matching handlers, falling back to the default
            handler when nothing else matched.
        """
        _version, mapping, default_handler = snapshot
        patterncompiled = self._compile_address_pattern(address_pattern)
        if patterncompiled is None:
            return []

        matched: List[Handler] = []
        seen: set[int] = set()
        for addr, handlers in mapping.items():
            if patterncompiled.match(addr) or (
                "*" in addr
                and re.match(addr.replace("*", ".*?") + "$", address_pattern)
            ):
                for handler in handlers:
                    if id(handler) in seen:
                        continue
                    seen.add(id(handler))
                    matched.append(handler)

        if not matched and default_handler is not None:
            logging.debug("No handler matched but default handler present, added it.")
            matched.append(default_handler)
        return matched

    def handlers_for_address(
        self, address_pattern: str
    ) -> Generator[Handler, None, None]:
        """Yield handlers matching an address in the current configuration.

        Each handler is yielded at most once per call. For dispatch, prefer the
        snapshot-based path so that a message keeps using one configuration
        version for its whole lifetime.

        Args:
            address_pattern: Address to match

        Returns:
            Generator yielding Handlers matching address_pattern
        """
        yield from self._matched_handlers(address_pattern, self._snapshot())

    def call_handlers_for_packet(
        self, data: bytes, client_address: Tuple[str, int]
    ) -> List:
        """Invoke handlers for all messages in OSC packet

        The incoming OSC Packet is decoded and the handlers for each included message is found and invoked.
        Each message captures one configuration snapshot before any of its handlers run, so a mapping
        added or removed by a handler cannot change the handlers of the message currently being
        processed; the change is observed by the next message. Each matched handler is invoked at
        most once per message, and a handler raising an exception is recorded in :attr:`failures`
        without interrupting the other matched handlers.

        Args:
            data: Data of packet
            client_address: Address of client this packet originated from
        Returns: A list of strings or tuples to be converted to OSC messages and returned to the client
        """
        results = list()
        # Get OSC messages from all bundles or standalone message.
        try:
            packet = osc_packet.OscPacket(data)
            for timed_msg in packet.messages:
                # Pin the configuration version for this message before any
                # handler runs. Remaps during invocation only affect later messages.
                snapshot = self._snapshot()
                now = time.time()
                handlers = self._matched_handlers(timed_msg.message.address, snapshot)
                if not handlers:
                    continue
                # If the message is to be handled later, then so be it.
                if self._strict_timing and timed_msg.time > now:
                    time.sleep(timed_msg.time - now)
                for handler in handlers:
                    try:
                        result = handler.invoke(client_address, timed_msg.message)
                    except Exception as error:
                        self._record_failure(
                            timed_msg.message.address,
                            handler,
                            client_address,
                            error,
                        )
                        continue
                    if result is not None:
                        results.append(result)
        except osc_packet.ParseError:
            pass
        return results

    async def async_call_handlers_for_packet(
        self, data: bytes, client_address: Tuple[str, int]
    ) -> List:
        """
        This function calls the handlers registered to the dispatcher for
        every message it found in the packet.
        The process/thread granularity is thus the OSC packet, not the handler.

        If parameters were registered with the dispatcher, then the handlers are
        called this way:
          handler('/address that triggered the message',
                  registered_param_list, osc_msg_arg1, osc_msg_arg2, ...)
        if no parameters were registered, then it is just called like this:
          handler('/address that triggered the message',
                  osc_msg_arg1, osc_msg_arg2, osc_msg_param3, ...)

        As with the synchronous path, each message pins one configuration snapshot before its
        handlers run, each matched handler runs at most once per message, and a raising handler
        is recorded in :attr:`failures` without cancelling the remaining handlers.
        Task cancellation (``asyncio.CancelledError``) is still allowed to propagate.
        """

        # Get OSC messages from all bundles or standalone message.
        results = []
        try:
            packet = osc_packet.OscPacket(data)
            for timed_msg in packet.messages:
                # Pin the configuration version for this message before any
                # handler runs. Remaps during invocation only affect later messages.
                snapshot = self._snapshot()
                now = time.time()
                handlers = self._matched_handlers(timed_msg.message.address, snapshot)
                if not handlers:
                    continue
                # If the message is to be handled later, then so be it.
                if self._strict_timing and timed_msg.time > now:
                    await asyncio.sleep(timed_msg.time - now)
                for handler in handlers:
                    try:
                        result = await handler.async_invoke(
                            client_address, timed_msg.message
                        )
                    except Exception as error:
                        self._record_failure(
                            timed_msg.message.address,
                            handler,
                            client_address,
                            error,
                        )
                        continue
                    if result is not None:
                        results.append(result)
        except osc_packet.ParseError:
            pass
        return results

    def set_default_handler(
        self, handler: Callable, needs_reply_address: bool = False
    ) -> None:
        """Sets the default handler

        The default handler is invoked every time no other handler is mapped to an address.

        Args:
            handler: Callback function to handle unmapped requests
            needs_reply_address: Whether the callback shall be passed the client address
        """
        with self._config_lock:
            self._default_handler = (
                None if (handler is None) else Handler(handler, [], needs_reply_address)
            )
            self._bump_version()
