"""Tests for per-response status, trust scope and source locations."""

# stdlib imports
import unittest
from io import BytesIO

# local imports
from ofxtools.models.bank.stmt import STMTRS
from ofxtools.models.response import (
    TRUST_COMPLETE,
    TRUST_NONE,
    TRUST_PARTIAL,
    StatementResponse,
    format_report,
    statement_report,
)
from ofxtools.Parser import OFXTree

OFX_V1_HEADER = (
    "OFXHEADER:100\r\n"
    "DATA:OFXSGML\r\n"
    "VERSION:102\r\n"
    "SECURITY:NONE\r\n"
    "ENCODING:USASCII\r\n"
    "CHARSET:1252\r\n"
    "COMPRESSION:NONE\r\n"
    "OLDFILEUID:NONE\r\n"
    "NEWFILEUID:NONE\r\n"
    "\r\n"
)

SONRS = """
<SIGNONMSGSRSV1>
<SONRS>
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20261115120000
<LANGUAGE>ENG
</SONRS>
</SIGNONMSGSRSV1>
"""


def bank_response(
    trnuid, code, severity, acctid=None, transactions=0, period=True, statement=True
):
    lines = ["<STMTTRNRS>", f"<TRNUID>{trnuid}"]
    lines.append(f"<STATUS><CODE>{code}<SEVERITY>{severity}")
    if code != 0:
        lines.append("<MESSAGE>Statement could not be completed")
    lines.append("</STATUS>")
    if not statement:
        lines.append("</STMTTRNRS>")
        return "\n".join(lines)
    lines.append("<STMTRS>")
    lines.append("<CURDEF>USD")
    if acctid is not None:
        lines.append(
            f"<BANKACCTFROM><BANKID>111<ACCTID>{acctid}"
            "<ACCTTYPE>CHECKING</BANKACCTFROM>"
        )
    if period:
        lines.append("<BANKTRANLIST>")
        lines.append("<DTSTART>20261001000000")
        lines.append("<DTEND>20261031235959")
        for n in range(transactions):
            lines.append(
                f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>2026100{n + 1}120000"
                f"<TRNAMT>-{n + 1}.00<FITID>{trnuid}-{n}<NAME>PAYEE{n}</STMTTRN>"
            )
        lines.append("</BANKTRANLIST>")
    lines.append("<LEDGERBAL><BALAMT>100.00<DTASOF>20261031235959</LEDGERBAL>")
    lines.append("</STMTRS>")
    lines.append("</STMTTRNRS>")
    return "\n".join(lines)


def make_ofx(*responses):
    body = "".join(responses)
    return (OFX_V1_HEADER + f"<OFX>{SONRS}<BANKMSGSRSV1>{body}</BANKMSGSRSV1></OFX>").encode()


def parse(source):
    parser = OFXTree()
    parser.parse(BytesIO(source))
    return parser.convert()


class MixedResponseTestCase(unittest.TestCase):
    def test_failed_partial_is_not_a_formal_statement(self):
        ofx = parse(
            make_ofx(
                bank_response("ok-account", 0, "INFO", "GOOD-1", transactions=1),
                bank_response(
                    "failed-with-data", 2000, "ERROR", "PARTIAL-2", transactions=1
                ),
                bank_response("failed-empty", 2001, "ERROR", statement=False),
            )
        )

        self.assertEqual(len(ofx.statements), 1)
        self.assertEqual(ofx.statements[0].account.acctid, "GOOD-1")
        self.assertEqual(len(ofx.statements[0].transactions), 1)

        scopes = [(r.trnuid, r.trusted_scope) for r in ofx.responses]
        self.assertEqual(
            scopes,
            [
                ("ok-account", TRUST_COMPLETE),
                ("failed-with-data", TRUST_PARTIAL),
                ("failed-empty", TRUST_NONE),
            ],
        )

        partial = ofx.responses[1]
        self.assertTrue(partial.failed)
        self.assertFalse(partial.formal)
        self.assertFalse(partial.complete_period)
        self.assertEqual(partial.account.acctid, "PARTIAL-2")
        self.assertEqual(len(partial.transactions), 1)
        self.assertIsNone(partial.formal_transactions)
        self.assertIsNotNone(partial.partial_transactions)
        self.assertFalse(partial.partial_data["complete_period"])
        self.assertIn("account", partial.trusted_fields)
        self.assertIn("completeness", partial.untrusted_fields)
        self.assertIn("period_coverage", partial.untrusted_fields)
        self.assertEqual(ofx.responses[0].untrusted_fields, [])

    def test_failed_block_with_no_transactions_is_not_zero_activity(self):
        ofx = parse(
            make_ofx(
                bank_response("empty-but-ok", 0, "INFO", "EMPTY-OK", transactions=0),
                bank_response("failed-empty-list", 2000, "ERROR", "EMPTY-BAD", 0),
            )
        )
        self.assertEqual(len(ofx.statements), 1)
        self.assertEqual(ofx.statements[0].account.acctid, "EMPTY-OK")
        self.assertEqual(len(ofx.statements[0].transactions), 0)
        self.assertTrue(ofx.responses[0].complete_period)

        failed = ofx.responses[1]
        self.assertEqual(failed.trusted_scope, TRUST_PARTIAL)
        self.assertFalse(failed.complete_period)
        self.assertIn(
            "not a valid zero-activity period",
            " ".join(failed.response_issues),
        )

    def test_successful_zero_transactions_is_a_complete_period(self):
        ofx = parse(
            make_ofx(bank_response("ok-empty", 0, "INFO", "EMPTY-OK", transactions=0))
        )
        self.assertEqual(len(ofx.statements), 1)
        self.assertEqual(ofx.statements[0].transactions, [])
        self.assertEqual(ofx.responses[0].trusted_scope, TRUST_COMPLETE)
        self.assertTrue(ofx.responses[0].complete_period)

    def test_partial_malformed_block_does_not_abort_siblings(self):
        # CURDEF is required; without it STMTRS cannot be model-converted.
        broken = bank_response(
            "malformed-partial", 2000, "ERROR", "BROKEN-2", transactions=1
        ).replace("<CURDEF>USD", "")
        ofx = parse(
            make_ofx(
                bank_response("ok-account", 0, "INFO", "GOOD-1", transactions=1),
                broken,
            )
        )

        self.assertEqual(len(ofx.statements), 1)
        self.assertEqual(ofx.statements[0].account.acctid, "GOOD-1")

        partial = ofx.responses[1]
        self.assertEqual(partial.trusted_scope, TRUST_PARTIAL)
        self.assertIn("curdef", partial.conversion_error)
        self.assertEqual(partial.account.acctid, "BROKEN-2")
        self.assertEqual(partial.transaction_count, 1)
        self.assertTrue(any("could not be fully converted" in i for i in partial.issues))

    def test_status_and_location_are_reported_per_response(self):
        ofx = parse(
            make_ofx(
                bank_response("ok-account", 0, "INFO", "GOOD-1", transactions=1),
                bank_response("failed-with-data", 2000, "ERROR", "PARTIAL-2", 1),
            )
        )

        report = ofx.response_report
        self.assertEqual(len(report), 2)
        for item in report:
            self.assertIsNotNone(item["location"])
            self.assertIsNotNone(item["source_location"]["path"])
            self.assertIsNotNone(item["source_location"]["line"])
        self.assertEqual(
            report[1]["source_location"]["path"],
            "OFX/BANKMSGSRSV1/STMTTRNRS[2]",
        )
        self.assertEqual(report[1]["status"]["code"], 2000)
        self.assertEqual(report[1]["status"]["severity"], "ERROR")
        self.assertFalse(report[1]["formal"])
        self.assertEqual(report[1]["trusted_scope"], TRUST_PARTIAL)

        summary = ofx.response_summary()
        self.assertIn("[complete]", summary)
        self.assertIn("[partial]", summary)
        self.assertIn("STMTTRNRS[2]", summary)

    def test_statement_response_types_are_exported(self):
        self.assertTrue(callable(StatementResponse.from_wrapper))
        self.assertEqual(statement_report([]), [])
        self.assertEqual(format_report([]), "")

    def test_statements_are_model_instances(self):
        ofx = parse(make_ofx(bank_response("ok", 0, "INFO", "GOOD-1", 1)))
        self.assertIsInstance(ofx.statements[0], STMTRS)
        self.assertEqual(ofx.statements[0].trusted_scope, TRUST_COMPLETE)
        self.assertEqual(ofx.statements[0].response_status.code, 0)
        self.assertEqual(ofx.statements[0].trnuid, "ok")


class StatusVariantsTestCase(unittest.TestCase):
    def test_warn_status_with_data_is_formal(self):
        ofx = parse(make_ofx(bank_response("warned", 0, "WARN", "WARN-1", 1)))
        response = ofx.responses[0]
        self.assertTrue(response.ok)
        self.assertEqual(response.trusted_scope, TRUST_COMPLETE)
        self.assertTrue(any("warning" in issue for issue in response.issues))
        self.assertEqual(len(ofx.statements), 1)

    def test_nonzero_code_is_failed_even_with_severity_info(self):
        ofx = parse(make_ofx(bank_response("code-only", 2000, "INFO", "BAD-1", 1)))
        response = ofx.responses[0]
        self.assertTrue(response.failed)
        self.assertEqual(response.trusted_scope, TRUST_PARTIAL)
        self.assertEqual(ofx.statements, [])


class ClientReportTestCase(unittest.TestCase):
    def test_request_statements_report(self):
        from unittest.mock import patch

        from ofxtools.Client import OFXClient

        markup = make_ofx(
            bank_response("ok-account", 0, "INFO", "GOOD-1", 1),
            bank_response("failed-with-data", 2000, "ERROR", "PARTIAL-2", 1),
        )
        client = OFXClient("https://example.com/ofx", org="ORG", fid="FID")
        with patch.object(client, "request_statements", return_value=BytesIO(markup)):
            response, report = client.request_statements_report("secret")
            self.assertEqual(response.read(), markup)

        self.assertEqual(len(report), 2)
        self.assertEqual(report[0]["trusted_scope"], TRUST_COMPLETE)
        self.assertEqual(report[1]["trusted_scope"], TRUST_PARTIAL)
        self.assertEqual(report[1]["status"]["code"], 2000)
        self.assertEqual(
            report[1]["source_location"]["path"],
            "OFX/BANKMSGSRSV1/STMTTRNRS[2]",
        )


class LocationTestCase(unittest.TestCase):
    def test_locations_use_body_offsets(self):
        parser = OFXTree()
        body = (
            f"<OFX>{SONRS}<BANKMSGSRSV1>"
            f"{bank_response('ok', 0, 'INFO', 'A', 1)}"
            "</BANKMSGSRSV1></OFX>"
        )
        parser.parse(BytesIO((OFX_V1_HEADER + body).encode()))
        tree = parser._root
        from ofxtools.location import get_source_location

        location = get_source_location(tree.find(".//STMTTRNRS"))
        self.assertIsNotNone(location)
        self.assertEqual(location.path, "OFX/BANKMSGSRSV1/STMTTRNRS")
        self.assertEqual(
            body[location.offset : location.offset + len("<STMTTRNRS>")],
            "<STMTTRNRS>",
        )
        # Line 1 is the root; SONRS is spread over lines.
        self.assertGreater(location.line, 1)
        line = body.splitlines()[location.line - 1]
        self.assertEqual(line[location.column - 1 :][: len("<STMTTRNRS>")], "<STMTTRNRS>")

    def test_location_indexes_repeated_siblings(self):
        ofx = parse(
            make_ofx(
                bank_response("one", 0, "INFO", "A", 1),
                bank_response("two", 0, "INFO", "B", 1),
            )
        )
        paths = [r.location.path for r in ofx.responses]
        self.assertEqual(
            paths,
            [
                "OFX/BANKMSGSRSV1/STMTTRNRS[1]",
                "OFX/BANKMSGSRSV1/STMTTRNRS[2]",
            ],
        )


if __name__ == "__main__":
    unittest.main()
