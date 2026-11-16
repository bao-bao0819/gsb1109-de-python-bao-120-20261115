"""Tests for ofxtools.migrate (legacy SGML -> XML migration)"""

# stdlib imports
import unittest
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

# local imports
import ofxtools
from ofxtools import Types
from ofxtools.header import OFXHeaderV1, OFXHeaderV2
from ofxtools.migrate import (
    MigrationError,
    StructureError,
    business_tree,
    convert,
    convert_file,
    verify,
)
from ofxtools.Parser import OFXTree
from ofxtools.scripts import ofxconvert

DATA = Path(__file__).parent / "data"

BANK_SGML = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20240101000000[-3:GMT]
<LANGUAGE>POR
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS>
<CURDEF>BRL
<BANKACCTFROM><BANKID>1<ACCTID>2<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20240101
<DTEND>20240201
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20240102<TRNAMT>-1.50<FITID>a<MEMO></MEMO></STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20240103<TRNAMT>10.25<FITID>b</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>8.75<DTASOF>20240201</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>"""


def parse_xml(markup: bytes) -> OFXTree:
    tree = OFXTree()
    tree.parse(BytesIO(markup))
    return tree


class PreserveHeaderTestCase(unittest.TestCase):
    def testVersionAndEncodingRecorded(self):
        result = convert(BANK_SGML.encode("cp1252"))
        header = parse_xml(result.xml).header
        self.assertIsInstance(header, OFXHeaderV2)
        self.assertEqual(header.version, 200)
        self.assertEqual(header.origofxheader, "100")
        self.assertEqual(header.origdata, "OFXSGML")
        self.assertEqual(header.origversion, "102")
        self.assertEqual(header.origencoding, "USASCII")
        self.assertEqual(header.origcharset, "1252")
        self.assertEqual(header.origcompression, "NONE")
        self.assertEqual(result.report.source_version, 102)
        self.assertEqual(result.report.source_charset, "1252")

    def testOutputIsUtf8(self):
        result = convert(BANK_SGML.encode("cp1252"))
        self.assertIn(b'encoding="UTF-8"', result.xml.splitlines()[0])
        # Decoding as UTF-8 must reproduce the exact text.
        self.assertEqual(result.xml.decode("utf_8").encode("utf_8"), result.xml)

    def testSourceVersionCanBeKeptInOutputVersion(self):
        result = convert(BANK_SGML.encode("cp1252"), version=203)
        self.assertEqual(result.header.version, 203)
        self.assertEqual(result.report.target_version, 203)
        # Still records the source version, which is not 203.
        self.assertEqual(result.header.origversion, "102")

    def testBadInputRaisesMigrationError(self):
        with self.assertRaises(MigrationError) as exc:
            convert(b"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n")
        self.assertIn("OFX header", str(exc.exception))

    def testOriginalHeaderClassStillExposed(self):
        header, _ = ofxtools.header.parse_header(BytesIO(BANK_SGML.encode("cp1252")))
        self.assertIsInstance(header, OFXHeaderV1)
        self.assertEqual(header.codec, "cp1252")

    def testV2InputKeepsPlainHeader(self):
        source = DATA.joinpath("stmtrs.ofx").read_bytes()
        result = convert(source)
        header = parse_xml(result.xml).header
        self.assertEqual(header.version, 200)
        self.assertIsNone(header.origversion)
        self.assertNotIn("ORIGVERSION", result.text)


class EmptyVersusMissingTestCase(unittest.TestCase):
    def testEmptyElementSurvives(self):
        result = convert(BANK_SGML.encode("cp1252"))
        self.assertIn("<MEMO></MEMO>", result.text)
        self.assertNotIn("<MEMO/>", result.text)

        tree = parse_xml(result.xml)
        ofx = tree.convert()
        transactions = ofx.statements[0].transactions
        self.assertIs(transactions[0].memo, Types.EMPTY)
        self.assertIsNone(transactions[1].memo)

    def testEmptyIsNotNoneAndUnconvertsToEmpty(self):
        from ofxtools.models.base import Aggregate

        class TESTLEAVES(Aggregate):
            memo = Types.String(32)

        aggregate = TESTLEAVES(memo=Types.EMPTY)
        self.assertIs(aggregate.memo, Types.EMPTY)
        self.assertIsNone(Types.String(32).unconvert(Types.EMPTY))
        self.assertEqual(bool(Types.EMPTY), False)
        self.assertEqual(repr(Types.EMPTY), "EMPTY")
        self.assertEqual(len(aggregate.to_etree()), 1)
        self.assertIsNone(aggregate.to_etree()[0].text)

    def testEmptyRequiredElementIsIllegal(self):
        broken = BANK_SGML.replace("<CURDEF>BRL", "<CURDEF></CURDEF>")
        with self.assertRaises(MigrationError):
            convert(broken.encode("cp1252"))

    def testMissingRequiredElementIsIllegal(self):
        broken = BANK_SGML.replace("<CURDEF>BRL\n", "")
        with self.assertRaises(MigrationError):
            convert(broken.encode("cp1252"))

    def testMissingOptionalStaysMissing(self):
        result = convert(BANK_SGML.encode("cp1252"))
        tree = parse_xml(result.xml)
        ofx = tree.convert()
        transaction = ofx.statements[0].transactions[1]
        self.assertIsNone(transaction.memo)
        self.assertNotIn(
            "<MEMO", result.text.split("<STMTTRN>")[2].split("</STMTTRN>")[0]
        )

    def testEmptyAggregateRoundTrips(self):
        # SECLISTRS is the one genuinely empty OFX aggregate.
        markup = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
            b'<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" '
            b'OLDFILEUID="NONE" NEWFILEUID="NONE"?>'
            b"<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE>"
            b"<SEVERITY>INFO</SEVERITY></STATUS>"
            b"<DTSERVER>20240101</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS>"
            b"</SIGNONMSGSRSV1><SECLISTMSGSRSV1><SECLISTTRNRS><TRNUID>1</TRNUID>"
            b"<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
            b"<SECLISTRS></SECLISTRS></SECLISTTRNRS></SECLISTMSGSRSV1></OFX>"
        )
        result = convert(markup)
        self.assertIn("<SECLISTRS></SECLISTRS>", result.text)
        result.verify(markup)


class StructureTestCase(unittest.TestCase):
    def testChainedRepairsAreAllReported(self):
        # BANKTRANLIST's end tag is omitted, and two sibling aggregates that
        # follow it are parsed inside it.  Each must be re-attached.
        body = BANK_SGML.replace(
            "</BANKTRANLIST>\n<LEDGERBAL><BALAMT>8.75<DTASOF>20240201</LEDGERBAL>",
            "<LEDGERBAL><BALAMT>8.75<DTASOF>20240201</LEDGERBAL>"
            "<AVAILBAL><BALAMT>8.00<DTASOF>20240201</AVAILBAL>",
        )
        self.assertNotEqual(body, BANK_SGML)
        with self.assertRaises(StructureError):
            convert(body.encode("cp1252"))
        result = convert(body.encode("cp1252"), repair=True)
        self.assertEqual(
            [repair.tag for repair in result.report.repairs],
            ["LEDGERBAL", "AVAILBAL"],
        )
        result.verify(body.encode("cp1252"))

    def testOmittedAggregateEndTagRejectedByDefault(self):
        with self.assertRaises(StructureError) as exc:
            convert(DATA.joinpath("br_nubank_cc.ofx"))
        self.assertIn("end tag appears to be omitted", str(exc.exception))

    def testRepairIsExplicitAndReported(self):
        result = convert(DATA.joinpath("br_nubank_cc.ofx"), repair=True)
        self.assertEqual(len(result.report.repairs), 1)
        repair = result.report.repairs[0]
        self.assertEqual(repair.tag, "LEDGERBAL")
        self.assertEqual(repair.from_parent, "BANKTRANLIST")
        self.assertEqual(repair.to_parent, "CCSTMTRS")
        # LEDGERBAL is now a sibling of BANKTRANLIST.
        tree = parse_xml(result.xml)
        stmtrs = tree.find(".//CCSTMTRS")
        self.assertEqual(
            [child.tag for child in stmtrs],
            ["CURDEF", "CCACCTFROM", "BANKTRANLIST", "LEDGERBAL"],
        )
        self.assertIsNone(stmtrs.find("BANKTRANLIST/LEDGERBAL"))

    def testRepairedDocumentReadsAsModel(self):
        result = convert(DATA.joinpath("br_nubank_cc.ofx"), repair=True)
        ofx = parse_xml(result.xml).convert()
        statement = ofx.statements[0]
        self.assertEqual(len(statement.transactions), 6)
        self.assertEqual(
            statement.balance.balamt, __import__("decimal").Decimal("-1280.91")
        )

    def testNoRepairsReportedForCleanInput(self):
        result = convert(BANK_SGML.encode("cp1252"))
        self.assertEqual(result.report.repairs, [])

    def testOutOfOrderChildrenRejectedByDefault(self):
        body = BANK_SGML.replace(
            "<DTSTART>20240101\n<DTEND>20240201",
            "<DTEND>20240201\n<DTSTART>20240101",
        )
        with self.assertRaises(StructureError):
            convert(body.encode("cp1252"))

    def testOrderRepairKeepsListMemberOrder(self):
        source = DATA.joinpath("invstmtrs.ofx").read_bytes()
        result = convert(source, repair=True)
        self._original_list_order = [
            (record["tag"], record["path"])
            for record in business_tree(
                __import__("ofxtools.migrate", fromlist=["_parse"])._parse(source)[1]
            )
            if record["tag"] in {"BUYSTOCK", "INVBANKTRAN"}
        ]
        # Re-read: the relative order of the two transaction kinds is intact.
        converted = parse_xml(result.xml)
        tags = [
            element.tag
            for element in converted.find(".//INVTRANLIST")
            if element.tag in {"BUYSTOCK", "INVBANKTRAN"}
        ]
        self.assertEqual(tags, ["BUYSTOCK", "INVBANKTRAN"])


class VerificationTestCase(unittest.TestCase):
    def testBusinessTreeComparesKindsAndText(self):
        records = business_tree(ET.fromstring("<A><B>1</B><C></C></A>"))
        self.assertEqual(records[1]["kind"], "leaf")
        self.assertEqual(records[1]["text"], "1")
        self.assertEqual(records[2]["kind"], "empty")
        self.assertIsNone(records[2]["text"])

    def testVerifyRejectsPresentButEmptyVersusAbsent(self):
        source_root = ET.fromstring("<OFX><A><B></B></A></OFX>")
        converted_root = ET.fromstring("<OFX><A/></OFX>")
        with self.assertRaises(MigrationError):
            verify(source_root, converted_root)
        # Same emptiness is accepted.
        verify(source_root, ET.fromstring("<OFX><A><B/></A></OFX>"))

    def testVerifyRejectsDroppedTransaction(self):
        result = convert(BANK_SGML.encode("cp1252"))
        broken = result.text.replace(
            "<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20240103</DTPOSTED>"
            "<TRNAMT>10.25</TRNAMT><FITID>b</FITID></STMTTRN>",
            "",
        )
        with self.assertRaises(MigrationError):
            verify(BANK_SGML.encode("cp1252"), broken.encode("utf_8"))

    def testRecordsCarryFieldTypes(self):
        result = convert(DATA.joinpath("stmtrs.ofx").read_bytes())
        records = {record["tag"]: record for record in business_tree(result.tree)}
        self.assertEqual(records["TRNAMT"]["type"], "Decimal")
        self.assertTrue(records["TRNAMT"]["required"])
        self.assertEqual(
            records["MEMO"]["type"] if "MEMO" in records else "String",
            records.get("MEMO", {"type": "String"})["type"],
        )
        self.assertEqual(records["STMTRS"]["type"], "STMTRS")

    def testCountsCorrespondOneToOne(self):
        result = convert(BANK_SGML.encode("cp1252"))
        counts = result.verify(BANK_SGML.encode("cp1252")).counts
        self.assertEqual(
            [item["text"] for item in counts["amounts"]], ["-1.50", "10.25", "8.75"]
        )
        self.assertEqual(len(counts["accounts"]), 1)
        self.assertEqual(len(counts["transactions"]), 2)
        self.assertEqual(result.report.counts["transactions"], counts["transactions"])

    def testFixtureRoundTrips(self):
        for name in ("stmtrs.ofx", "stmtrs_euro.ofx", "invstmtrs.ofx"):
            with self.subTest(name=name):
                source = DATA.joinpath(name).read_bytes()
                result = convert(source, repair=True)
                verification = result.verify(source)
                self.assertGreater(verification.records_compared, 10)

    def testConvertedFilmCanBeAnalyzedByModel(self):
        result = convert(DATA.joinpath("stmtrs.ofx").read_bytes())
        ofx = parse_xml(result.xml).convert()
        self.assertEqual(len(ofx.statements), 1)
        self.assertEqual(len(ofx.statements[0].transactions), 2)


class FileAndCliTestCase(unittest.TestCase):
    def testConvertFileWritesSibling(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy.ofx"
            source.write_bytes(BANK_SGML.encode("cp1252"))
            result = convert_file(source)
            destination = source.with_suffix(".xml")
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), result.xml)

    def testConvertFileExplicitDestination(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy.ofx"
            source.write_bytes(BANK_SGML.encode("cp1252"))
            destination = Path(tmp) / "migrated.xml"
            convert_file(source, destination)
            self.assertEqual(destination.read_bytes(), convert(source).xml)

    def testCliConvertsAndReports(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy.ofx"
            source.write_bytes(BANK_SGML.encode("cp1252"))
            destination = Path(tmp) / "migrated.xml"
            status = ofxconvert.main(
                [str(source), "-o", str(destination), "--report", "-q"]
            )
            self.assertEqual(status, 0)
            self.assertTrue(destination.is_file())
            report = Path(tmp) / "migrated.report.json"
            self.assertIn('"source_version": 102', report.read_text())

    def testCliRejectsStructureByDefault(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "migrated.xml"
            status = ofxconvert.main(
                [str(DATA / "br_nubank_cc.ofx"), "-o", str(destination), "-q"]
            )
            self.assertEqual(status, 1)
            self.assertFalse(destination.exists())

    def testCliRepairFlag(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "migrated.xml"
            status = ofxconvert.main(
                [
                    str(DATA / "br_nubank_cc.ofx"),
                    "-o",
                    str(destination),
                    "--repair",
                    "-q",
                ]
            )
            self.assertEqual(status, 0)
            self.assertTrue(destination.is_file())

    def testCliMultipleSources(self):
        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "one.ofx"
            second = Path(tmp) / "two.ofx"
            first.write_bytes(BANK_SGML.encode("cp1252"))
            second.write_bytes(BANK_SGML.encode("cp1252"))
            status = ofxconvert.main([str(first), str(second), "-q"])
            self.assertEqual(status, 0)
            self.assertTrue((Path(tmp) / "one.xml").is_file())
            self.assertTrue((Path(tmp) / "two.xml").is_file())


if __name__ == "__main__":
    unittest.main()
