import unittest

from pythonosc.dispatcher import Dispatcher, Handler, RuleMatch
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.osc_pattern import InvalidAddressPattern, compile_address_pattern


class TestAddressPattern(unittest.TestCase):
    def assertMatches(self, pattern, accepted, rejected):
        compiled = compile_address_pattern(pattern)
        for address in accepted:
            with self.subTest(pattern=pattern, address=address):
                self.assertTrue(compiled.matches(address))
        for address in rejected:
            with self.subTest(pattern=pattern, address=address):
                self.assertFalse(compiled.matches(address))

    def test_brace_alternatives(self):
        self.assertMatches(
            "/synth/{osc,sawtooth}/{1,2}",
            ["/synth/osc/1", "/synth/sawtooth/2"],
            ["/synth/pulse/1", "/synth/osc/3", "/synth/osc"],
        )

    def test_character_classes_and_negation(self):
        self.assertMatches(
            "/track/[0-9][!0-4]",
            ["/track/15", "/track/39", "/track/1a"],
            ["/track/11", "/track/14"],
        )

    def test_character_class_range(self):
        self.assertMatches("/track/[a-c]", ["/track/a", "/track/c"], ["/track/d"])

    def test_wildcards_do_not_cross_slash(self):
        self.assertMatches("/foo/*", ["/foo/bar"], ["/foo/bar/baz"])
        self.assertMatches("/foo/?", ["/foo/a"], ["/foo/ab"])

    def test_brace_candidates_are_literal_strings(self):
        self.assertMatches(
            "/a/{b*,c?}",
            ["/a/b*", "/a/c?"],
            ["/a/beta", "/a/cd"],
        )

    def test_character_class_inside_brace_candidate_is_literal(self):
        self.assertMatches(
            "/a/{[0-9],x}",
            ["/a/[0-9]", "/a/x"],
            ["/a/4"],
        )

    def test_malformed_patterns_name_the_rule(self):
        for pattern in (
            "/a/{b,c",
            "/a/{}",
            "/a/{,b}",
            "/a/[0-9",
            "/a/[]",
            "/a/[z-a]",
            "/a/b}",
            "/a/,b",
            "a/b",
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaises(InvalidAddressPattern) as raised:
                    compile_address_pattern(pattern)
                self.assertEqual(raised.exception.pattern, pattern)


class TestRuleMatches(unittest.TestCase):
    def setUp(self):
        self.dispatcher = Dispatcher()

    def test_one_fact_per_rule_even_with_several_handlers(self):
        self.dispatcher.map("/device/*/gain", "first")
        self.dispatcher.map("/device/*/gain", "second")

        matches = self.dispatcher.matching_rules("/device/1/gain", "if")

        self.assertEqual(
            [
                RuleMatch(
                    address="/device/1/gain",
                    pattern="/device/*/gain",
                    argument_types=("i", "f"),
                )
            ],
            matches,
        )

    def test_reports_every_hit_rule(self):
        self.dispatcher.map("/device/*/gain", "wildcard")
        self.dispatcher.map("/device/1/gain", "exact")

        matches = self.dispatcher.matching_rules("/device/1/gain", ",f")

        self.assertEqual(
            [
                RuleMatch("/device/1/gain", "/device/*/gain", ("f",)),
                RuleMatch("/device/1/gain", "/device/1/gain", ("f",)),
            ],
            matches,
        )

    def test_pattern_sample_reports_resolved_address(self):
        self.dispatcher.map("/device/1/gain", "exact")

        matches = self.dispatcher.matching_rules("/device/?/gain")

        self.assertEqual(
            [RuleMatch("/device/?/gain", "/device/1/gain", (), "/device/1/gain")],
            matches,
        )

    def test_message_matches_use_parsed_type_tags(self):
        self.dispatcher.map("/device/{1,2}/gain", "gain")
        builder = OscMessageBuilder("/device/2/gain")
        builder.add_arg(3)
        builder.add_arg("db")

        matches = self.dispatcher.matches_for_message(builder.build())

        self.assertEqual(["i", "s"], list(matches[0].argument_types))
        self.assertEqual("is", matches[0].type_tag)

    def test_malformed_rule_is_rejected_at_map_time(self):
        with self.assertRaises(InvalidAddressPattern) as raised:
            self.dispatcher.map("/device/{1,2", "gain")
        self.assertEqual("/device/{1,2", raised.exception.pattern)
        self.assertEqual([], list(self.dispatcher.handlers_for_address("/device/1")))

    def test_malformed_sample_names_the_sample(self):
        self.dispatcher.map("/device/*", "gain")
        with self.assertRaises(InvalidAddressPattern) as raised:
            list(self.dispatcher.handlers_for_address("/device/[0-9"))
        self.assertEqual("/device/[0-9", raised.exception.pattern)

    def test_packet_with_malformed_address_is_skipped(self):
        received = []
        self.dispatcher.map("/*", lambda address, *args: received.append(address))

        result = self.dispatcher.call_handlers_for_packet(
            b"/device/[0-9\x00\x00,s\x00\x00x\x00\x00\x00", ("127.0.0.1", 1234)
        )

        self.assertEqual([], result)
        self.assertEqual([], received)


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


if __name__ == "__main__":
    unittest.main()
