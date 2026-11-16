"""Unit tests for ofxtools.Sync coverage tracking."""

# stdlib imports
import datetime
import json
import tempfile
import unittest
from pathlib import Path

# local imports
from ofxtools.Client import StmtRq
from ofxtools.Sync import (
    CoverageLedger,
    DateRange,
    Synchronizer,
    build_coverage_table,
    coverage_from_response,
    month_range,
)
from ofxtools.utils import UTC

SIGNON = """
<SIGNONMSGSRSV1>
  <SONRS>
    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20260930120000</DTSERVER>
    <LANGUAGE>ENG</LANGUAGE>
  </SONRS>
</SIGNONMSGSRSV1>
"""


def statement_response(
    status_code=0,
    severity="INFO",
    message=None,
    dtstart="20260901",
    dtend="20260930",
    transactions=(),
    include_tranlist=True,
    acctid="123456789",
):
    """Build an OFXv2 bank statement response as bytes for parser coverage."""
    status_message = f"<MESSAGE>{message}</MESSAGE>" if message else ""
    tranlist = ""
    if include_tranlist:
        tranlist = f"""
        <BANKTRANLIST>
          <DTSTART>{dtstart}</DTSTART>
          <DTEND>{dtend}</DTEND>
          {''.join(transactions)}
        </BANKTRANLIST>
        """
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
{SIGNON}
<BANKMSGSRSV1>
  <STMTTRNRS>
    <TRNUID>1001</TRNUID>
    <STATUS>
      <CODE>{status_code}</CODE>
      <SEVERITY>{severity}</SEVERITY>
      {status_message}
    </STATUS>
    <STMTRS>
      <CURDEF>USD</CURDEF>
      <BANKACCTFROM>
        <BANKID>121099999</BANKID>
        <ACCTID>{acctid}</ACCTID>
        <ACCTTYPE>CHECKING</ACCTTYPE>
      </BANKACCTFROM>
      {tranlist}
      <LEDGERBAL>
        <BALAMT>200.29</BALAMT>
        <DTASOF>20260930120000</DTASOF>
      </LEDGERBAL>
    </STMTRS>
  </STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
""".encode()


def tx(fitid, dtposted, amount="-10.00"):
    return f"""
    <STMTTRN>
      <TRNTYPE>DEBIT</TRNTYPE>
      <DTPOSTED>{dtposted}</DTPOSTED>
      <TRNAMT>{amount}</TRNAMT>
      <FITID>{fitid}</FITID>
    </STMTTRN>
    """


def closing_response(dtpoststart="20260901", dtpostend="20260930", status_code=0, severity="INFO"):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
{SIGNON}
<BANKMSGSRSV1>
  <STMTENDTRNRS>
    <TRNUID>1002</TRNUID>
    <STATUS><CODE>{status_code}</CODE><SEVERITY>{severity}</SEVERITY></STATUS>
    <STMTENDRS>
      <CURDEF>USD</CURDEF>
      <BANKACCTFROM>
        <BANKID>121099999</BANKID>
        <ACCTID>123456789</ACCTID>
        <ACCTTYPE>CHECKING</ACCTTYPE>
      </BANKACCTFROM>
      <CLOSING>
        <FITID>close-1</FITID>
        <DTCLOSE>20260930</DTCLOSE>
        <BALCLOSE>200.29</BALCLOSE>
        <DTPOSTSTART>{dtpoststart}</DTPOSTSTART>
        <DTPOSTEND>{dtpostend}</DTPOSTEND>
      </CLOSING>
    </STMTENDRS>
  </STMTENDTRNRS>
</BANKMSGSRSV1>
</OFX>
""".encode()


def signon_warning_response(statement_severity="INFO", statement_message=None):
    message = f"<MESSAGE>{statement_message}</MESSAGE>" if statement_message else ""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1>
    <SONRS>
      <STATUS><CODE>0</CODE><SEVERITY>WARN</SEVERITY><MESSAGE>Signon notice</MESSAGE></STATUS>
      <DTSERVER>20260930120000</DTSERVER>
      <LANGUAGE>ENG</LANGUAGE>
    </SONRS>
  </SIGNONMSGSRSV1>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <TRNUID>1001</TRNUID>
      <STATUS><CODE>0</CODE><SEVERITY>{statement_severity}</SEVERITY>{message}</STATUS>
      <STMTRS>
        <CURDEF>USD</CURDEF>
        <BANKACCTFROM>
          <BANKID>121099999</BANKID>
          <ACCTID>123456789</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST><DTSTART>20260901</DTSTART><DTEND>20260930</DTEND></BANKTRANLIST>
        <LEDGERBAL><BALAMT>200.29</BALAMT><DTASOF>20260930</DTASOF></LEDGERBAL>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
""".encode()


def mixed_response(statements):
    """Build one BANKMSGSRSV1 response containing arbitrary *STMTTRNRS blocks."""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
{SIGNON}
<BANKMSGSRSV1>
{''.join(statements)}
</BANKMSGSRSV1>
</OFX>
""".encode()


def statement_block(acctid="123456789", status_code=0, severity="INFO", message=None,
                    dtstart="20260901", dtend="20260930"):
    message_xml = f"<MESSAGE>{message}</MESSAGE>" if message else ""
    return f"""
  <STMTTRNRS>
    <TRNUID>1001</TRNUID>
    <STATUS><CODE>{status_code}</CODE><SEVERITY>{severity}</SEVERITY>{message_xml}</STATUS>
    <STMTRS>
      <CURDEF>USD</CURDEF>
      <BANKACCTFROM>
        <BANKID>121099999</BANKID><ACCTID>{acctid}</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE>
      </BANKACCTFROM>
      <BANKTRANLIST><DTSTART>{dtstart}</DTSTART><DTEND>{dtend}</DTEND></BANKTRANLIST>
      <LEDGERBAL><BALAMT>200.29</BALAMT><DTASOF>20260930</DTASOF></LEDGERBAL>
    </STMTRS>
  </STMTTRNRS>
"""


def error_block(message="Statement unavailable"):
    return f"""
  <STMTTRNRS>
    <TRNUID>1001</TRNUID>
    <STATUS><CODE>2000</CODE><SEVERITY>ERROR</SEVERITY><MESSAGE>{message}</MESSAGE></STATUS>
  </STMTTRNRS>
"""


def error_without_statement_response():
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
{SIGNON}
<BANKMSGSRSV1>
  <STMTTRNRS>
    <TRNUID>1001</TRNUID>
    <STATUS><CODE>2000</CODE><SEVERITY>ERROR</SEVERITY><MESSAGE>Statement unavailable</MESSAGE></STATUS>
  </STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
""".encode()


class DateRangeTestCase(unittest.TestCase):
    def testInclusiveBoundaries(self):
        interval = DateRange(
            datetime.datetime(2026, 9, 1, tzinfo=UTC),
            datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        self.assertTrue(interval.contains(interval.start))
        self.assertTrue(interval.contains(interval.end))
        self.assertIn(interval.end, interval)

    def testSubtract(self):
        whole = DateRange(
            datetime.datetime(2026, 9, 1, tzinfo=UTC),
            datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        partial = DateRange(
            datetime.datetime(2026, 9, 10, tzinfo=UTC),
            datetime.datetime(2026, 9, 20, tzinfo=UTC),
        )
        gaps = whole.subtract(partial)
        self.assertEqual(len(gaps), 2)
        self.assertEqual(gaps[0].start, whole.start)
        self.assertEqual(gaps[1].end, whole.end)

    def testNaiveDatetimeIsUtc(self):
        interval = DateRange(datetime.datetime(2026, 9, 1), datetime.datetime(2026, 9, 2))
        self.assertEqual(interval.start.tzinfo, UTC)
        self.assertEqual(interval.end.tzinfo, UTC)

    def testSubtractDateOnlyUsesWholeDay(self):
        requested = DateRange(
            datetime.datetime(2026, 9, 1, tzinfo=UTC),
            datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        declared = DateRange(
            datetime.datetime(2026, 9, 1, tzinfo=UTC),
            datetime.datetime(2026, 9, 20, tzinfo=UTC),
        )
        gap = requested.subtract(declared)[0]
        self.assertEqual(gap.start.date(), datetime.date(2026, 9, 21))
        self.assertEqual(gap.end.date(), datetime.date(2026, 9, 30))

    def testMonthRange(self):
        interval = month_range(2026, 9)
        self.assertEqual(interval.start, datetime.datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(interval.end, datetime.datetime(2026, 9, 30, tzinfo=UTC))
        self.assertEqual(
            interval.end, datetime.datetime(2026, 9, 30, tzinfo=UTC)
        )


class CoverageRowTestCase(unittest.TestCase):
    def setUp(self):
        self.request = StmtRq(
            acctid="123456789",
            accttype="CHECKING",
            dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
            dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )

    def testRequestedDeclaredReceivedAreDistinct(self):
        transaction = tx("abc", "20260915", "-12.34")
        response = statement_response(
            dtstart="20260910",
            dtend="20260920",
            transactions=(transaction,),
        )
        row = coverage_from_response(response, self.request)

        self.assertEqual(row.requested.start, self.request.dtstart)
        self.assertEqual(row.declared.start.date(), datetime.date(2026, 9, 10))
        self.assertEqual(row.declared.end.date(), datetime.date(2026, 9, 20))
        self.assertEqual(row.record_count, 1)
        self.assertEqual(row.records[0].fitid, "abc")
        self.assertEqual(
            row.received_range.start.date(), datetime.date(2026, 9, 15)
        )

        self.assertFalse(row.is_complete)
        self.assertFalse(row.closes_gap)
        self.assertIsNotNone(row.gap)
        self.assertEqual(row.gap.start, self.request.dtstart)

    def testFullDeclarationWithRecordsClosesGap(self):
        response = statement_response(
            transactions=(tx("abc", "20260915"), tx("def", "20260920")),
        )
        row = coverage_from_response(response, self.request)
        self.assertTrue(row.is_complete)
        self.assertTrue(row.closes_gap)
        self.assertEqual(row.gaps, ())
        self.assertEqual(row.unique_fitids, ("abc", "def"))

    def testEmptyListWithFullDeclarationClosesGap(self):
        response = statement_response()
        row = coverage_from_response(response, self.request)
        self.assertEqual(row.record_count, 0)
        self.assertIsNotNone(row.declared)
        self.assertTrue(row.is_complete)
        self.assertTrue(row.closes_gap)
        self.assertEqual(row.gaps, ())

    def testEmptyListWithoutDeclarationCannotCloseGap(self):
        response = statement_response(include_tranlist=False)
        row = coverage_from_response(response, self.request)
        self.assertEqual(row.record_count, 0)
        self.assertIsNone(row.declared)
        self.assertFalse(row.is_complete)
        self.assertFalse(row.closes_gap)
        self.assertEqual(row.gaps, (row.requested,))

    def testWarningIsRecordedButDoesNotImplyError(self):
        response = statement_response(
            status_code=0,
            severity="WARN",
            message="Results are incomplete",
            dtstart="20260910",
            dtend="20260920",
        )
        row = coverage_from_response(response, self.request)
        self.assertTrue(row.is_warning)
        self.assertFalse(row.is_error)
        self.assertEqual(row.status.warning_message, "Results are incomplete")
        self.assertIn("Results are incomplete", row.warnings)
        self.assertFalse(row.closes_gap)

    def testWarningWithFullCoverageClosesGap(self):
        response = statement_response(
            severity="WARN",
            message="Some pending items omitted",
        )
        row = coverage_from_response(response, self.request)
        self.assertTrue(row.is_warning)
        self.assertTrue(row.closes_gap)

    def testErrorResponseReportsErrorAndNoCoverage(self):
        response = statement_response(
            status_code=2000,
            severity="ERROR",
            message="Statement unavailable",
            include_tranlist=False,
        )
        row = coverage_from_response(response, self.request)
        self.assertTrue(row.is_error)
        self.assertEqual(row.error_message, "Statement unavailable")
        self.assertIsNone(row.accepted_coverage)
        self.assertFalse(row.closes_gap)

    def testErrorResponseIgnoresEvenFullDeclaredCoverage(self):
        response = statement_response(
            status_code=2000,
            severity="ERROR",
            message="Server error",
        )
        row = coverage_from_response(response, self.request)
        self.assertTrue(row.is_error)
        self.assertTrue(row.declared_complete)
        self.assertFalse(row.is_complete)
        self.assertIsNone(row.accepted_coverage)
        self.assertFalse(row.closes_gap)


class CoverageTableTestCase(unittest.TestCase):
    def testTableRenderingAndExport(self):
        request = StmtRq(
            acctid="123456789",
            dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
            dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        response = statement_response(
            dtstart="20260910", dtend="20260920", transactions=(tx("abc", "20260915"),)
        )
        table = build_coverage_table(response, [request])
        self.assertEqual(len(table), 1)
        text = str(table)
        self.assertIn("Requested", text)
        self.assertIn("Declared", text)
        self.assertIn("Records", text)
        self.assertIn("no", text)

        markdown = table.to_markdown()
        self.assertIn("2026-09-01", markdown)
        self.assertIn("2026-09-10", markdown)

        csv_text = table.to_csv()
        self.assertIn("requested_start", csv_text)
        self.assertIn("declared_start", csv_text)
        self.assertIn("record_count", csv_text)

        payload = table.to_dict()
        self.assertEqual(payload["row_count"], 1)
        self.assertFalse(payload["complete"])

    def testClosingStatementDeclaresPostingWindow(self):
        request = StmtRq(
            acctid="123456789",
            dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
            dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        table = build_coverage_table(closing_response(), [request])
        row = table[0]
        self.assertEqual(row.account, "123456789")
        self.assertEqual(row.declared.start.date(), datetime.date(2026, 9, 1))
        self.assertEqual(row.declared.end.date(), datetime.date(2026, 9, 30))
        self.assertEqual(row.record_count, 0)
        self.assertTrue(row.closes_gap)

    def testSignonWarningBlocksCoverage(self):
        request = StmtRq(
            acctid="123456789",
            dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
            dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        table = build_coverage_table(signon_warning_response(), [request])
        row = table[0]
        self.assertTrue(row.is_warning)
        self.assertIn("Signon notice", row.warnings)
        # The statement may be complete, but a qualified signon is not
        # authoritative evidence for the ledger.
        self.assertTrue(row.declared_complete)
        self.assertFalse(row.is_complete)
        self.assertFalse(row.closes_gap)
        self.assertIsNone(row.accepted_coverage)

        ledger = CoverageLedger()
        update = ledger.apply(row)
        self.assertFalse(update.accepted)
        self.assertFalse(update.checkpoint_advanced)
        self.assertEqual(update.reason, "coverage blocked by signon status")

    def testErrorWithoutStatementPayloadKeepsRequestRow(self):
        request = StmtRq(
            acctid="123456789",
            dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
            dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        table = build_coverage_table(error_without_statement_response(), [request])
        row = table[0]
        self.assertEqual(row.account, "123456789")
        self.assertEqual(row.requested.start.date(), datetime.date(2026, 9, 1))
        self.assertTrue(row.is_error)
        self.assertFalse(row.closes_gap)

    def testMultipleErrorResponsesWithoutStatementsDoNotDuplicateRows(self):
        requests = [
            StmtRq(
                acctid="111",
                dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
                dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
            ),
            StmtRq(
                acctid="222",
                dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
                dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
            ),
        ]
        response = error_without_statement_response().replace(b"<TRNUID>1001</TRNUID>", b"<TRNUID>1001</TRNUID>")
        # Duplicate the error wrapper so both requests have a response.
        body = response.decode()
        wrapper = body[body.index("<STMTTRNRS>"):body.index("</STMTTRNRS>") + len("</STMTTRNRS>")]
        body = body.replace(wrapper, wrapper + wrapper)
        response = body.encode()

        table = build_coverage_table(response, requests)
        self.assertEqual(len(table), 2)
        self.assertEqual([row.account for row in table], ["111", "222"])
        self.assertTrue(all(row.is_error for row in table))
        self.assertTrue(all(not row.closes_gap for row in table))

    def testMixedErrorAndSuccessMatchByAccount(self):
        requests = [
            StmtRq(acctid="111", dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC), dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC)),
            StmtRq(acctid="222", dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC), dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC)),
        ]
        response = mixed_response([
            error_block(),
            statement_block(acctid="222"),
        ])
        table = build_coverage_table(response, requests)
        by_account = {row.account: row for row in table}

        self.assertEqual(set(by_account), {"111", "222"})
        self.assertTrue(by_account["111"].is_error)
        self.assertFalse(by_account["111"].closes_gap)
        self.assertFalse(by_account["222"].is_error)
        self.assertTrue(by_account["222"].closes_gap)

    def testMismatchedAccountProducesGapRow(self):
        other_request = StmtRq(
            acctid="111",
            dtstart=datetime.datetime(2026, 9, 1, tzinfo=UTC),
            dtend=datetime.datetime(2026, 9, 30, tzinfo=UTC),
        )
        # The response belongs to a different account.  Do not attribute its
        # declared coverage to the requested account.
        response = statement_response(acctid="222")
        table = build_coverage_table(response, [other_request])

        self.assertEqual(len(table), 2)
        requested_row = next(row for row in table if row.account == "111")
        self.assertTrue(requested_row.is_error)
        self.assertEqual(requested_row.record_count, 0)
        self.assertFalse(requested_row.closes_gap)

        unmatched_row = next(row for row in table if row.account == "222")
        self.assertIsNone(unmatched_row.requested)
        self.assertEqual(unmatched_row.record_count, 0)


class CoverageLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.interval = month_range(2026, 9)
        self.request = StmtRq(
            acctid="123456789",
            dtstart=self.interval.start,
            dtend=self.interval.end,
        )

    def testPartialCoverageDoesNotAdvanceCheckpoint(self):
        response = statement_response(dtstart="20260910", dtend="20260920")
        table = build_coverage_table(response, [self.request])
        ledger = CoverageLedger()
        update = ledger.apply(table[0])

        self.assertTrue(update.accepted)
        self.assertIsNone(update.checkpoint_before)
        self.assertIsNone(update.checkpoint_after)
        self.assertFalse(update.checkpoint_advanced)
        self.assertEqual(len(update.gaps), 2)
        self.assertEqual(ledger.checkpoint("123456789"), None)
        self.assertEqual(len(ledger.coverage("123456789")), 1)

    def testSubsequentCompleteMonthCanAdvanceCheckpoint(self):
        response = statement_response(dtstart="20260910", dtend="20260920")
        table = build_coverage_table(response, [self.request])
        ledger = CoverageLedger()
        first = ledger.apply(table[0])
        self.assertFalse(first.checkpoint_advanced)

        # A later complete month advances over its own full range; the earlier
        # partial month remains visible as historical coverage but is not
        # silently treated as a continuous prefix.
        october = month_range(2026, 10)
        october_request = StmtRq(
            acctid="123456789", dtstart=october.start, dtend=october.end
        )
        second = ledger.apply(
            build_coverage_table(
                statement_response(dtstart="20261001", dtend="20261031"),
                [october_request],
            )[0]
        )
        self.assertTrue(second.checkpoint_advanced)
        self.assertEqual(second.checkpoint_after, october.end)

    def testDisjointPartialResponsesStillDoNotAdvance(self):
        ledger = CoverageLedger()
        pieces = (("20260901", "20260910"), ("20260921", "20260930"))
        for start, end in pieces:
            row = build_coverage_table(
                statement_response(dtstart=start, dtend=end), [self.request]
            )[0]
            update = ledger.apply(row)
            self.assertFalse(update.checkpoint_advanced)
        self.assertEqual(ledger.checkpoint("123456789"), None)
        self.assertEqual(len(ledger.coverage("123456789")), 2)
        self.assertEqual(len(ledger.gaps("123456789", self.interval)), 1)
        gap = ledger.gaps("123456789", self.interval)[0]
        self.assertEqual(gap.start.date(), datetime.date(2026, 9, 11))
        self.assertEqual(gap.end.date(), datetime.date(2026, 9, 20))

    def testCompleteEmptyListAdvancesCheckpoint(self):
        response = statement_response()
        table = build_coverage_table(response, [self.request])
        ledger = CoverageLedger()
        update = ledger.apply(table[0])

        self.assertTrue(update.accepted)
        self.assertTrue(update.checkpoint_advanced)
        self.assertEqual(update.gaps, ())
        self.assertEqual(ledger.checkpoint("123456789"), self.interval.end)
        self.assertEqual(ledger.gaps("123456789", self.interval), ())

    def testErrorDoesNotAdvanceCheckpoint(self):
        ledger = CoverageLedger()
        ok = build_coverage_table(statement_response(), [self.request])[0]
        first = ledger.apply(ok)
        self.assertTrue(first.checkpoint_advanced)
        checkpoint = first.checkpoint_after

        error_response = statement_response(
            status_code=2000,
            severity="ERROR",
            message="Server error",
        )
        error_table = build_coverage_table(error_response, [self.request])
        update = ledger.apply(error_table[0])

        self.assertFalse(update.accepted)
        self.assertFalse(update.checkpoint_advanced)
        self.assertEqual(update.checkpoint_before, checkpoint)
        self.assertEqual(update.checkpoint_after, checkpoint)
        self.assertEqual(ledger.checkpoint("123456789"), checkpoint)
        # The pre-existing coverage interval is retained unchanged.
        self.assertEqual(ledger.coverage("123456789"), (self.interval,))

    def testErrorWithFullDeclarationStillDoesNotAdvance(self):
        response = statement_response(
            status_code=2000,
            severity="ERROR",
            message="Server error",
        )
        row = build_coverage_table(response, [self.request])[0]
        self.assertTrue(row.declared_complete)
        self.assertFalse(row.is_complete)
        ledger = CoverageLedger()
        update = ledger.apply(row)
        self.assertFalse(update.accepted)
        self.assertIsNone(update.checkpoint_after)
        self.assertEqual(ledger.coverage("123456789"), ())

    def testContiguousCoverageAdvancesPastPreviousCheckpoint(self):
        ledger = CoverageLedger()
        august = month_range(2026, 8)
        august_request = StmtRq(acctid="123456789", dtstart=august.start, dtend=august.end)
        september_request = StmtRq(acctid="123456789", dtstart=self.interval.start, dtend=self.interval.end)
        ledger.apply(build_coverage_table(statement_response(dtstart="20260801", dtend="20260831"), [august_request])[0])
        self.assertEqual(ledger.checkpoint("123456789"), august.end)

        update = ledger.apply(build_coverage_table(statement_response(), [september_request])[0])
        self.assertEqual(update.checkpoint_before, august.end)
        self.assertEqual(update.checkpoint_after, self.interval.end)

    def testLedgerJsonRoundtrip(self):
        ledger = CoverageLedger()
        ledger.apply(build_coverage_table(statement_response(), [self.request])[0])
        payload = ledger.to_dict()
        restored = CoverageLedger.from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(restored.checkpoint("123456789"), self.interval.end)
        self.assertEqual(restored.coverage("123456789"), ledger.coverage("123456789"))

    def testLedgerFileRoundtrip(self):
        ledger = CoverageLedger()
        ledger.apply(build_coverage_table(statement_response(), [self.request])[0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger.save(path)
            restored = CoverageLedger.load(path)
            self.assertEqual(restored.checkpoint("123456789"), self.interval.end)


class SynchronizerTestCase(unittest.TestCase):
    def testIngestBuildsTableAndUpdatesLedger(self):
        interval = month_range(2026, 9)
        request = StmtRq(
            acctid="123456789", dtstart=interval.start, dtend=interval.end
        )
        synchronizer = Synchronizer()
        batch = synchronizer.ingest(statement_response(), [request])
        self.assertTrue(batch.complete)
        self.assertEqual(batch.updates[0].checkpoint_after, interval.end)

    def testRequestMonthSetsFullMonthRange(self):
        interval = month_range(2026, 9)
        request = StmtRq(acctid="123456789")

        class FakeClient:
            def request_statements(self, password, *requests, **kwargs):
                self.requests = requests
                return FakeResponse(statement_response())

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.payload

        client = FakeClient()
        synchronizer = Synchronizer(client)
        batch = synchronizer.request_month("secret", request, 2026, 9)
        self.assertEqual(client.requests[0].dtstart, interval.start)
        self.assertEqual(client.requests[0].dtend, interval.end)
        self.assertTrue(batch.complete)
        self.assertEqual(
            synchronizer.ledger.checkpoint("123456789"), interval.end
        )

    def testNetworkErrorDoesNotAdvanceCheckpoint(self):
        interval = month_range(2026, 9)
        request = StmtRq(
            acctid="123456789", dtstart=interval.start, dtend=interval.end
        )

        class FakeClient:
            def request_statements(self, *args, **kwargs):
                raise OSError("connection reset")

        synchronizer = Synchronizer(FakeClient())
        batch = synchronizer.request("secret", request)
        self.assertTrue(batch.rows[0].is_error)
        self.assertEqual(batch.rows[0].error_message, "connection reset")
        self.assertIsNone(synchronizer.ledger.checkpoint("123456789"))
        self.assertEqual(synchronizer.ledger.coverage("123456789"), ())


if __name__ == "__main__":
    unittest.main()
