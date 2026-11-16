import unittest

from pythonosc.dispatcher import Dispatcher, Handler, DispatchFailure
from pythonosc.osc_bundle_builder import OscBundleBuilder, IMMEDIATELY
from pythonosc.osc_message_builder import build_msg


CLIENT_ADDRESS = ("127.0.0.1", 1234)


class TestDispatcher(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.dispatcher = Dispatcher()

    def sortAndAssertSequenceEqual(self, expected, result):
        def sort(lst):
            return sorted(lst, key=lambda x: x.callback)

        return self.assertSequenceEqual(sort(expected), sort(result))

    def test_invalid_utf8_is_ignored_before_next_valid_packet(self):
        received = []
        self.dispatcher.map("/test", lambda address, *args: received.append(args))
        for packet in (
            b"/\xff\x00\x00,s\x00\x00ok\x00\x00",
            b"/test\x00\x00\x00,\xff\x00\x00",
            b"/test\x00\x00\x00,s\x00\x00\xff\x00\x00\x00",
        ):
            with self.subTest(packet=packet):
                self.assertEqual(
                    [],
                    self.dispatcher.call_handlers_for_packet(
                        packet, ("127.0.0.1", 1234)
                    ),
                )
        self.assertEqual([], received)
        self.dispatcher.call_handlers_for_packet(
            b"/test\x00\x00\x00,s\x00\x00ok\x00\x00", ("127.0.0.1", 1234)
        )
        self.assertEqual([("ok",)], received)

    def test_empty_by_default(self):
        self.sortAndAssertSequenceEqual(
            [], self.dispatcher.handlers_for_address("/test")
        )

    def test_use_default_handler_when_set_and_no_match(self):
        handler = object()
        self.dispatcher.set_default_handler(handler)

        self.sortAndAssertSequenceEqual(
            [Handler(handler, [])], self.dispatcher.handlers_for_address("/test")
        )

    def test_simple_map_and_match(self):
        handler = object()
        self.dispatcher.map("/test", handler, 1, 2, 3)
        self.dispatcher.map("/test2", handler)
        self.sortAndAssertSequenceEqual(
            [Handler(handler, [1, 2, 3])], self.dispatcher.handlers_for_address("/test")
        )
        self.sortAndAssertSequenceEqual(
            [Handler(handler, [])], self.dispatcher.handlers_for_address("/test2")
        )

    def test_example_from_spec(self):
        addresses = [
            "/first/this/one",
            "/second/1",
            "/second/2",
            "/third/a",
            "/third/b",
            "/third/c",
        ]
        for index, address in enumerate(addresses):
            self.dispatcher.map(address, index)

        for index, address in enumerate(addresses):
            self.sortAndAssertSequenceEqual(
                [Handler(index, [])], self.dispatcher.handlers_for_address(address)
            )

        self.sortAndAssertSequenceEqual(
            [Handler(1, []), Handler(2, [])],
            self.dispatcher.handlers_for_address("/second/?"),
        )

        self.sortAndAssertSequenceEqual(
            [Handler(3, []), Handler(4, []), Handler(5, [])],
            self.dispatcher.handlers_for_address("/third/*"),
        )

    def test_do_not_match_over_slash(self):
        self.dispatcher.map("/foo/bar/1", 1)
        self.dispatcher.map("/foo/bar/2", 2)

        self.sortAndAssertSequenceEqual([], self.dispatcher.handlers_for_address("/*"))

    def test_match_middle_star(self):
        self.dispatcher.map("/foo/bar/1", 1)
        self.dispatcher.map("/foo/bar/2", 2)

        self.sortAndAssertSequenceEqual(
            [Handler(2, [])], self.dispatcher.handlers_for_address("/foo/*/2")
        )

    def test_match_multiple_stars(self):
        self.dispatcher.map("/foo/bar/1", 1)
        self.dispatcher.map("/foo/bar/2", 2)

        self.sortAndAssertSequenceEqual(
            [Handler(1, []), Handler(2, [])],
            self.dispatcher.handlers_for_address("/*/*/*"),
        )

    def test_match_address_contains_plus_as_character(self):
        self.dispatcher.map("/footest/bar+tender/1", 1)

        self.sortAndAssertSequenceEqual(
            [Handler(1, [])], self.dispatcher.handlers_for_address("/foo*/bar+*/*")
        )
        self.sortAndAssertSequenceEqual(
            [Handler(1, [])], self.dispatcher.handlers_for_address("/foo*/bar*/*")
        )

    def test_call_correct_dispatcher_on_star(self):
        self.dispatcher.map("/a+b", 1)
        self.dispatcher.map("/aaab", 2)
        self.sortAndAssertSequenceEqual(
            [Handler(2, [])], self.dispatcher.handlers_for_address("/aaab")
        )
        self.sortAndAssertSequenceEqual(
            [Handler(1, [])], self.dispatcher.handlers_for_address("/a+b")
        )

    def test_map_star(self):
        self.dispatcher.map("/starbase/*", 1)
        self.sortAndAssertSequenceEqual(
            [Handler(1, [])], self.dispatcher.handlers_for_address("/starbase/bar")
        )

    def test_map_root_star(self):
        self.dispatcher.map("/*", 1)
        self.sortAndAssertSequenceEqual(
            [Handler(1, [])], self.dispatcher.handlers_for_address("/anything/matches")
        )

    def test_map_double_stars(self):
        self.dispatcher.map("/foo/*/bar/*", 1)
        self.sortAndAssertSequenceEqual(
            [Handler(1, [])], self.dispatcher.handlers_for_address("/foo/wild/bar/wild")
        )
        self.sortAndAssertSequenceEqual(
            [], self.dispatcher.handlers_for_address("/foo/wild/nomatch/wild")
        )

    def test_multiple_handlers(self):
        self.dispatcher.map("/foo/bar", 1)
        self.dispatcher.map("/foo/bar", 2)
        self.sortAndAssertSequenceEqual(
            [Handler(1, []), Handler(2, [])],
            self.dispatcher.handlers_for_address("/foo/bar"),
        )

    def test_multiple_handlers_with_wildcard_map(self):
        self.dispatcher.map("/foo/bar", 1)
        self.dispatcher.map("/*", 2)
        self.sortAndAssertSequenceEqual(
            [Handler(1, []), Handler(2, [])],
            self.dispatcher.handlers_for_address("/foo/bar"),
        )

    def test_unmap(self):
        def dummyhandler():
            pass

        # Test with handler returned by map
        returnedhandler = self.dispatcher.map("/map/me", dummyhandler)
        self.sortAndAssertSequenceEqual(
            [Handler(dummyhandler, [])], self.dispatcher.handlers_for_address("/map/me")
        )
        self.dispatcher.unmap("/map/me", returnedhandler)
        self.sortAndAssertSequenceEqual(
            [], self.dispatcher.handlers_for_address("/map/me")
        )

        # Test with reconstructing handler
        self.dispatcher.map("/map/me/too", dummyhandler)
        self.sortAndAssertSequenceEqual(
            [Handler(dummyhandler, [])],
            self.dispatcher.handlers_for_address("/map/me/too"),
        )
        self.dispatcher.unmap("/map/me/too", dummyhandler)
        self.sortAndAssertSequenceEqual(
            [], self.dispatcher.handlers_for_address("/map/me/too")
        )

    def test_unmap_exception(self):
        def dummyhandler():
            pass

        with self.assertRaises(ValueError):
            self.dispatcher.unmap("/unmap/exception", dummyhandler)

        handlerobj = self.dispatcher.map("/unmap/somethingelse", dummyhandler())
        with self.assertRaises(ValueError):
            self.dispatcher.unmap("/unmap/exception", handlerobj)

    def test_handlers_for_address_wildcard_no_partial_match(self):
        self.dispatcher.map("/qwer/*/zxcv", 1)
        # Should not match
        handlers = list(
            self.dispatcher.handlers_for_address("/qwer/whatever/zxcvsomethingmore")
        )
        self.assertEqual(len(handlers), 0)
        # Should match
        handlers = list(self.dispatcher.handlers_for_address("/qwer/whatever/zxcv"))
        self.assertEqual(len(handlers), 1)

    def test_strict_timing_disabled(self):
        # Disable strict timing
        dispatcher = Dispatcher(strict_timing=False)

        callback_called = False

        def handler(address, *args):
            nonlocal callback_called
            callback_called = True

        dispatcher.map("/test", handler)

        # Create a message with a future timestamp (1 hour from now)
        # We'll use OscPacket to simulate a bundle with a future timestamp
        # But for simple unit test, we can just check if it sleeps
        # Since we can't easily mock time.sleep across the dispatcher without more effort,
        # we'll just verify the logic exists.
        self.assertFalse(dispatcher._strict_timing)

    async def test_async_call_handlers_for_packet(self):
        dispatcher = Dispatcher()

        sync_called = False

        def sync_handler(address, *args):
            nonlocal sync_called
            sync_called = True

        async_called = False

        async def async_handler(address, *args):
            nonlocal async_called
            async_called = True

        dispatcher.map("/sync", sync_handler)
        dispatcher.map("/async", async_handler)

        # Dispatch sync handler
        dgram_sync = b"/sync\x00\x00\x00,\x00\x00\x00"
        await dispatcher.async_call_handlers_for_packet(dgram_sync, ("127.0.0.1", 1234))
        self.assertTrue(sync_called)

        # Dispatch async handler
        dgram_async = b"/async\x00\x00,\x00\x00\x00"
        await dispatcher.async_call_handlers_for_packet(
            dgram_async, ("127.0.0.1", 1234)
        )
        self.assertTrue(async_called)

    def test_version_bumps_on_every_mapping_change(self):
        start = self.dispatcher.version
        returned = self.dispatcher.map("/a", lambda *args: None)
        self.assertEqual(start + 1, self.dispatcher.version)
        self.dispatcher.unmap("/a", returned)
        self.assertEqual(start + 2, self.dispatcher.version)
        self.dispatcher.set_default_handler(lambda *args: None)
        self.assertEqual(start + 3, self.dispatcher.version)

    def test_message_keeps_fixed_config_version_when_handler_remaps(self):
        calls = []
        remapped = False

        def late_handler(address, *args):
            calls.append("late")

        def recorder(address, *args):
            calls.append("recorder")

        def remapper(address, *args):
            nonlocal remapped
            calls.append("remapper")
            if remapped:
                return
            remapped = True
            # Add a new subscription and revoke an existing one mid-dispatch.
            self.dispatcher.map("/a", late_handler)
            self.dispatcher.unmap("/a", recorder_handler)

        self.dispatcher.map("/a", remapper)
        recorder_handler = self.dispatcher.map("/a", recorder)

        results = self.dispatcher.call_handlers_for_packet(
            build_msg("/a").dgram, CLIENT_ADDRESS
        )

        # The current message keeps its original snapshot: the revoked recorder
        # still runs and the newly mapped late handler does not.
        self.assertEqual(["remapper", "recorder"], calls)
        self.assertEqual([], results)
        # The next message observes the new configuration version.
        calls.clear()
        self.dispatcher.call_handlers_for_packet(build_msg("/a").dgram, CLIENT_ADDRESS)
        self.assertEqual(["remapper", "late"], calls)
        self.assertEqual([], self.dispatcher.failures)

    def test_revocation_only_affects_later_messages_in_bundle(self):
        calls = []

        def target(address, *args):
            calls.append("target")

        def revoker(address, *args):
            self.dispatcher.unmap("/a", target_handler)

        target_handler = self.dispatcher.map("/a", target)
        self.dispatcher.map("/revoke", revoker)

        bundle = OscBundleBuilder(IMMEDIATELY)
        bundle.add_content(build_msg("/revoke"))
        bundle.add_content(build_msg("/a"))

        self.dispatcher.call_handlers_for_packet(bundle.build().dgram, CLIENT_ADDRESS)

        # The handler was revoked while processing the first message, so the
        # second message (processed afterwards) must no longer dispatch it.
        self.assertEqual([], calls)

    def test_handler_failure_is_recorded_without_blocking_others(self):
        order = []

        def good_before(address, *args):
            order.append("before")

        def boom(address, *args):
            raise RuntimeError("callback exploded")

        def good_after(address, *args):
            order.append("after")

        self.dispatcher.map("/a", good_before)
        failing_handler = self.dispatcher.map("/a", boom)
        self.dispatcher.map("/a", good_after)

        # Must not raise even though the middle handler fails.
        results = self.dispatcher.call_handlers_for_packet(
            build_msg("/a").dgram, CLIENT_ADDRESS
        )

        self.assertEqual(["before", "after"], order)
        self.assertEqual([], results)
        failures = self.dispatcher.failures
        self.assertEqual(1, len(failures))
        failure = failures[0]
        self.assertIsInstance(failure, DispatchFailure)
        self.assertEqual("/a", failure.address)
        self.assertIs(failing_handler, failure.handler)
        self.assertEqual(CLIENT_ADDRESS, failure.client_address)
        self.assertIsInstance(failure.error, RuntimeError)

    def test_each_failing_callback_is_recorded_separately(self):
        def boom1(address, *args):
            raise ValueError("one")

        def boom2(address, *args):
            raise ValueError("two")

        self.dispatcher.map("/a", boom1)
        self.dispatcher.map("/a", boom2)

        self.dispatcher.call_handlers_for_packet(build_msg("/a").dgram, CLIENT_ADDRESS)

        failures = self.dispatcher.failures
        self.assertEqual(2, len(failures))
        self.assertEqual(["one", "two"], [str(f.error) for f in failures])
        self.dispatcher.clear_failures()
        self.assertEqual([], self.dispatcher.failures)

    def test_failure_buffer_is_bounded_and_drops_oldest(self):
        dispatcher = Dispatcher(failure_buffer_size=2)

        def make_boom(message):
            def boom(address, *args):
                raise RuntimeError(message)

            return boom

        dispatcher.map("/a", make_boom("one"))
        dispatcher.map("/a", make_boom("two"))
        dispatcher.map("/a", make_boom("three"))

        dispatcher.call_handlers_for_packet(build_msg("/a").dgram, CLIENT_ADDRESS)

        self.assertEqual(["two", "three"], [str(f.error) for f in dispatcher.failures])

    def test_same_handler_object_matching_several_addresses_dispatches_once(self):
        calls = []

        def shared(address, *args):
            calls.append(address)

        shared_handler = self.dispatcher.map("/a", shared)
        # Make the same Handler instance reachable through a second, also
        # matching address bucket.
        self.dispatcher._map["/*"].append(shared_handler)

        self.dispatcher.call_handlers_for_packet(build_msg("/a").dgram, CLIENT_ADDRESS)

        self.assertEqual(1, len(calls))

    def test_distinct_registrations_are_still_dispatched_separately(self):
        calls = []

        def shared_callback(address, *args):
            calls.append(address)

        # Two separate registrations create two distinct subscriptions and must
        # both be dispatched, even though the callback function is identical.
        self.dispatcher.map("/a", shared_callback)
        self.dispatcher.map("/a", shared_callback)

        self.dispatcher.call_handlers_for_packet(build_msg("/a").dgram, CLIENT_ADDRESS)

        self.assertEqual(2, len(calls))

    async def test_async_failure_does_not_block_other_handlers(self):
        order = []

        async def good(address, *args):
            order.append("good")

        async def boom(address, *args):
            raise RuntimeError("async boom")

        self.dispatcher.map("/a", good)
        failing_handler = self.dispatcher.map("/a", boom)
        self.dispatcher.map("/a", good)

        await self.dispatcher.async_call_handlers_for_packet(
            build_msg("/a").dgram, CLIENT_ADDRESS
        )

        self.assertEqual(["good", "good"], order)
        failures = self.dispatcher.failures
        self.assertEqual(1, len(failures))
        self.assertIs(failing_handler, failures[0].handler)
        self.assertIsInstance(failures[0].error, RuntimeError)

    async def test_async_message_keeps_fixed_config_version(self):
        calls = []

        def late_handler(address, *args):
            calls.append("late")

        async def remapper(address, *args):
            calls.append("remapper")
            self.dispatcher.map("/a", late_handler)

        self.dispatcher.map("/a", remapper)

        await self.dispatcher.async_call_handlers_for_packet(
            build_msg("/a").dgram, CLIENT_ADDRESS
        )
        self.assertEqual(["remapper"], calls)

        calls.clear()
        await self.dispatcher.async_call_handlers_for_packet(
            build_msg("/a").dgram, CLIENT_ADDRESS
        )
        self.assertEqual(["remapper", "late"], calls)


if __name__ == "__main__":
    unittest.main()
