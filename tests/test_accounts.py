"""Unit tests for ofxtools.accounts"""

# stdlib imports
import datetime
import os
import unittest
from decimal import Decimal

# local imports
from ofxtools import models
from ofxtools.accounts import (
    AccountKey,
    AccountRegistry,
    NumberingPeriod,
    scan,
)
from ofxtools.models.bank.stmt import BANKACCTFROM, CCACCTFROM
from ofxtools.models.invest.acct import INVACCTFROM
from ofxtools.Parser import OFXTree
from ofxtools.utils import UTC

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def parse_ofx(filename):
    parser = OFXTree()
    with open(os.path.join(DATA_DIR, filename), "rb") as f:
        parser.parse(f)
    return parser.convert()


class AccountKeyTestCase(unittest.TestCase):
    def test_str_parse_roundtrip(self):
        key = AccountKey(
            domain="BANK", institution="121099999", acctid="999988", accttype="CHECKING"
        )
        self.assertEqual(str(key), "BANK:121099999:CHECKING:999988")
        self.assertEqual(AccountKey.parse(str(key)), key)

    def test_str_parse_roundtrip_no_institution(self):
        key = AccountKey(domain="CREDITCARD", institution=None, acctid="4111")
        self.assertEqual(str(key), "CREDITCARD:::4111")
        self.assertEqual(AccountKey.parse(str(key)), key)

    def test_domain_normalized_uppercase(self):
        key = AccountKey(domain="bank", institution="1", acctid="2")
        self.assertEqual(key.domain, "BANK")

    def test_invalid_domain_rejected(self):
        with self.assertRaises(ValueError):
            AccountKey(domain="BROKERAGE", institution="1", acctid="2")

    def test_empty_acctid_rejected(self):
        with self.assertRaises(ValueError):
            AccountKey(domain="BANK", institution="1", acctid="")

    def test_blank_institution_rejected(self):
        with self.assertRaises(ValueError):
            AccountKey(domain="BANK", institution="  ", acctid="2")

    def test_parse_garbage_rejected(self):
        with self.assertRaises(ValueError):
            AccountKey.parse("not-a-key")

    def test_same_acctid_different_banks_distinct_keys(self):
        # Two banks may issue the same account number
        key1 = AccountKey(
            domain="BANK", institution="121099999", acctid="999988", accttype="CHECKING"
        )
        key2 = AccountKey(
            domain="BANK", institution="021000021", acctid="999988", accttype="CHECKING"
        )
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(str(key1), str(key2))

    def test_same_acctid_different_domains_distinct_keys(self):
        # Securities accounts live in a different naming domain
        key1 = AccountKey(domain="BANK", institution="121099999", acctid="999988")
        key2 = AccountKey(domain="INVESTMENT", institution="121099999", acctid="999988")
        self.assertNotEqual(key1, key2)


class AccountKeyFromAcctfromTestCase(unittest.TestCase):
    def test_bankacctfrom(self):
        acctfrom = BANKACCTFROM(
            bankid="121099999", acctid="999988", accttype="CHECKING"
        )
        key = AccountKey.from_acctfrom(acctfrom)
        self.assertEqual(key.domain, "BANK")
        self.assertEqual(key.institution, "121099999")
        self.assertEqual(key.acctid, "999988")
        self.assertEqual(key.accttype, "CHECKING")

    def test_invacctfrom(self):
        acctfrom = INVACCTFROM(brokerid="121099999", acctid="999988")
        key = AccountKey.from_acctfrom(acctfrom, accttype="NORMAL")
        self.assertEqual(key.domain, "INVESTMENT")
        self.assertEqual(key.institution, "121099999")
        self.assertEqual(key.accttype, "NORMAL")

    def test_ccacctfrom_uses_fid_evidence(self):
        acctfrom = CCACCTFROM(acctid="4111")
        key = AccountKey.from_acctfrom(acctfrom, org="NCH", fid="1001")
        self.assertEqual(key.domain, "CREDITCARD")
        self.assertEqual(key.institution, "1001")

    def test_ccacctfrom_falls_back_to_org(self):
        acctfrom = CCACCTFROM(acctid="4111")
        key = AccountKey.from_acctfrom(acctfrom, org="NCH")
        self.assertEqual(key.institution, "NCH")

    def test_ccacctfrom_no_evidence_is_unattributed(self):
        acctfrom = CCACCTFROM(acctid="4111")
        key = AccountKey.from_acctfrom(acctfrom)
        self.assertIsNone(key.institution)

    def test_unknown_aggregate_rejected(self):
        with self.assertRaises(TypeError):
            AccountKey.from_acctfrom(object())


class NumberingPeriodTestCase(unittest.TestCase):
    def test_covers_unbounded(self):
        period = NumberingPeriod(acctid="1")
        self.assertTrue(period.covers(None))
        self.assertTrue(period.covers(datetime.date(1999, 1, 1)))

    def test_covers_half_open_interval(self):
        period = NumberingPeriod(
            acctid="1",
            valid_from=datetime.date(2020, 1, 1),
            valid_to=datetime.date(2021, 6, 1),
        )
        self.assertFalse(period.covers(datetime.date(2019, 12, 31)))
        self.assertTrue(period.covers(datetime.date(2020, 1, 1)))
        self.assertTrue(period.covers(datetime.date(2021, 5, 31)))
        self.assertFalse(period.covers(datetime.date(2021, 6, 1)))
        self.assertFalse(period.covers(None))

    def test_inverted_interval_rejected(self):
        with self.assertRaises(ValueError):
            NumberingPeriod(
                acctid="1",
                valid_from=datetime.date(2021, 1, 1),
                valid_to=datetime.date(2020, 1, 1),
            )


class AccountRegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = AccountRegistry()
        self.key = AccountKey(
            domain="BANK", institution="121099999", acctid="999988", accttype="CHECKING"
        )

    def test_register_and_keys_sorted(self):
        other = AccountKey(domain="BANK", institution="021000021", acctid="1")
        self.registry.register(self.key)
        self.registry.register(other)
        self.assertEqual(self.registry.keys, sorted([self.key, other], key=str))

    def test_unattributed_kept_separate(self):
        orphan = AccountKey(domain="CREDITCARD", institution=None, acctid="4111")
        self.registry.register(orphan)
        self.registry.register(orphan)  # idempotent
        self.assertEqual(self.registry.unattributed, [orphan])
        self.assertEqual(self.registry.keys, [])

    def test_explicit_alias(self):
        self.registry.register_alias("house-checking", self.key)
        self.assertIs(self.registry.lookup_alias("house-checking"), self.key)
        self.assertEqual(self.registry.aliases, {"house-checking": self.key})

    def test_unknown_alias_raises(self):
        with self.assertRaises(KeyError):
            self.registry.lookup_alias("nope")

    def test_blank_alias_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register_alias("  ", self.key)

    def test_alias_of_unattributed_rejected(self):
        orphan = AccountKey(domain="CREDITCARD", institution=None, acctid="4111")
        with self.assertRaises(ValueError):
            self.registry.register_alias("card", orphan)

    def test_renumbering_resolves_by_validity_period(self):
        renumbered = datetime.date(2021, 6, 1)
        canonical = self.registry.record_renumbering(self.key, "777555", renumbered)
        self.assertEqual(canonical.acctid, "777555")

        old_observation = AccountKey(
            domain="BANK", institution="121099999", acctid="999988", accttype="CHECKING"
        )
        # Historical observations of the old number resolve to the new key
        self.assertEqual(
            self.registry.resolve(old_observation, asof=datetime.date(2020, 1, 1)),
            canonical,
        )
        self.assertEqual(
            self.registry.resolve(old_observation, asof=datetime.date(2021, 5, 31)),
            canonical,
        )
        # Old number observed after the cutoff does not resolve
        self.assertEqual(
            self.registry.resolve(old_observation, asof=datetime.date(2021, 6, 1)),
            old_observation,
        )
        # New number resolves from its effective date onward
        self.assertEqual(
            self.registry.resolve(canonical, asof=datetime.date(2021, 6, 1)),
            canonical,
        )
        self.assertEqual(self.registry.resolve(canonical), canonical)

    def test_resolve_without_history_is_identity(self):
        self.assertEqual(self.registry.resolve(self.key), self.key)

    def test_renumbering_unattributed_rejected(self):
        orphan = AccountKey(domain="CREDITCARD", institution=None, acctid="4111")
        with self.assertRaises(ValueError):
            self.registry.record_renumbering(
                orphan, "4222", datetime.date(2021, 1, 1)
            )

    def test_renumbering_noncurrent_rejected(self):
        self.registry.record_renumbering(self.key, "777555", datetime.date(2021, 6, 1))
        with self.assertRaises(ValueError):
            self.registry.record_renumbering(
                self.key, "333111", datetime.date(2022, 1, 1)
            )


def make_cc_ofx(acctid="4111-1111-1111-1111"):
    """Credit card statement response whose SONRS carries no FI evidence."""
    status = models.STATUS(code=0, severity="INFO")
    sonrs = models.SONRS(
        status=status,
        dtserver=datetime.datetime(2024, 4, 2, tzinfo=UTC),
        language="ENG",
    )
    ccstmtrs = models.CCSTMTRS(
        curdef="BRL",
        ccacctfrom=models.CCACCTFROM(acctid=acctid),
        ledgerbal=models.LEDGERBAL(
            balamt=Decimal("-100.00"),
            dtasof=datetime.datetime(2024, 4, 2, tzinfo=UTC),
        ),
    )
    ccstmttrnrs = models.CCSTMTTRNRS(trnuid="1001", status=status, ccstmtrs=ccstmtrs)
    return models.OFX(
        signonmsgsrsv1=models.SIGNONMSGSRSV1(sonrs=sonrs),
        creditcardmsgsrsv1=models.CREDITCARDMSGSRSV1(ccstmttrnrs),
    )


class ScanTestCase(unittest.TestCase):
    def test_scan_bank_statement(self):
        ofx = parse_ofx("stmtrs.ofx")
        keys, unattributed = scan(ofx)
        self.assertEqual(unattributed, [])
        self.assertEqual(
            keys,
            [
                AccountKey(
                    domain="BANK",
                    institution="121099999",
                    acctid="999988",
                    accttype="CHECKING",
                )
            ],
        )

    def test_scan_investment_statement_distinct_domain(self):
        # Same institution/acctid as stmtrs.ofx, different naming domain
        ofx = parse_ofx("invstmtrs.ofx")
        keys, unattributed = scan(ofx)
        self.assertEqual(unattributed, [])
        self.assertEqual(
            keys,
            [AccountKey(domain="INVESTMENT", institution="121099999", acctid="999988")],
        )

    def test_scan_creditcard_without_fi_is_unattributed(self):
        # No FI in the signon: no institution evidence.  The account is
        # listed separately; attribution is not guessed from the file name.
        ofx = make_cc_ofx()
        keys, unattributed = scan(ofx)
        self.assertEqual(keys, [])
        self.assertEqual(len(unattributed), 1)
        key = unattributed[0]
        self.assertEqual(key.domain, "CREDITCARD")
        self.assertIsNone(key.institution)
        self.assertEqual(key.acctid, "4111-1111-1111-1111")

    def test_scan_with_registry_resolves_renumbering(self):
        registry = AccountRegistry()
        old_key = AccountKey(
            domain="BANK", institution="121099999", acctid="999988", accttype="CHECKING"
        )
        canonical = registry.record_renumbering(
            old_key, "777555", datetime.date(2006, 1, 1)
        )
        ofx = parse_ofx("stmtrs.ofx")  # statement ends 2005-10-28
        keys, unattributed = scan(ofx, registry=registry)
        self.assertEqual(unattributed, [])
        self.assertEqual(keys, [canonical])

    def test_scan_registers_unattributed(self):
        registry = AccountRegistry()
        scan(make_cc_ofx(), registry=registry)
        self.assertEqual(len(registry.unattributed), 1)
        self.assertEqual(registry.keys, [])


if __name__ == "__main__":
    unittest.main()
