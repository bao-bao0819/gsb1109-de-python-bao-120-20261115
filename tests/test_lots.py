"""Unit tests for ofxtools.lots - corporate-action lot migration"""

# stdlib imports
import unittest
from decimal import Decimal

# local imports
from ofxtools.lots import (
    CashProceeds,
    Lot,
    LotAction,
    LotMigrator,
    SecurityProceeds,
    migrate_lots,
)

D = Decimal
Q = D("0.00000001")


def make_lot(
    lot_id: str = "L1",
    secid: str = "OLD",
    units: str = "100",
    cost: str = "1000.00",
    account: str | None = "ACCT",
) -> Lot:
    return Lot(
        lot_id=lot_id, secid=secid, units=D(units), cost=D(cost), account=account
    )


def by_secid(lots):
    return {lot.secid: lot for lot in lots}


class LotTestCase(unittest.TestCase):
    def testValidation(self):
        with self.assertRaises(ValueError):
            Lot(lot_id="", secid="X", units=D(1), cost=D(1))
        with self.assertRaises(ValueError):
            Lot(lot_id="L", secid="", units=D(1), cost=D(1))
        with self.assertRaises(ValueError):
            Lot(lot_id="L", secid="X", units=D(0), cost=D(1))
        with self.assertRaises(ValueError):
            Lot(lot_id="L", secid="X", units=D(1), cost=D(-1))

    def testProceedsValidation(self):
        with self.assertRaises(ValueError):
            SecurityProceeds(secid="N", ratio=D(0), cost_fraction=D(1))
        with self.assertRaises(ValueError):
            SecurityProceeds(secid="N", ratio=D(1), cost_fraction=D(2))
        with self.assertRaises(ValueError):
            CashProceeds(amount=D(-1), cost_fraction=D(0))
        with self.assertRaises(ValueError):
            CashProceeds(amount=D(1), cost_fraction=D(2))

    def testActionValidation(self):
        good = SecurityProceeds(secid="N", ratio=D(1), cost_fraction=D(1))
        with self.assertRaises(ValueError):
            LotAction(action_id="A", secid="OLD", order=0)
        with self.assertRaises(ValueError):
            LotAction(
                action_id="A",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="N", ratio=D(1), cost_fraction=D("0.5"))
                ],
            )
        with self.assertRaises(ValueError):
            LotAction(
                action_id="A",
                secid="OLD",
                order=0,
                securities=[good, good],
            )
        # Fractions summing exactly to 1 is accepted.
        LotAction(action_id="A", secid="OLD", order=0, securities=[good])


class MigratorValidationTestCase(unittest.TestCase):
    def testDuplicateActionId(self):
        proceed = SecurityProceeds(secid="N", ratio=D(1), cost_fraction=D(1))
        actions = [
            LotAction(action_id="A", secid="OLD", order=0, securities=[proceed]),
            LotAction(action_id="A", secid="OTHER", order=0, securities=[proceed]),
        ]
        with self.assertRaises(ValueError):
            LotMigrator(actions)

    def testDuplicateOrderOnSamePredecessor(self):
        proceed = SecurityProceeds(secid="N", ratio=D(1), cost_fraction=D(1))
        actions = [
            LotAction(action_id="A", secid="OLD", order=0, securities=[proceed]),
            LotAction(action_id="B", secid="OLD", order=0, securities=[proceed]),
        ]
        with self.assertRaises(ValueError):
            LotMigrator(actions)


class BasicMigrationTestCase(unittest.TestCase):
    def testOneActionTwoNewSecurities(self):
        actions = [
            LotAction(
                action_id="MERGER",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="NEWA", ratio=D("0.5"),
                                     cost_fraction=D("0.7")),
                    SecurityProceeds(secid="NEWB", ratio=D("0.25"),
                                     cost_fraction=D("0.3")),
                ],
            )
        ]
        result = migrate_lots([make_lot()], actions)

        self.assertEqual(len(result.new_lots), 2)
        lots = by_secid(result.new_lots)
        self.assertEqual(lots["NEWA"].units, D("50.00000000"))
        self.assertEqual(lots["NEWA"].cost, D("700.00000000"))
        self.assertEqual(lots["NEWB"].units, D("25.00000000"))
        self.assertEqual(lots["NEWB"].cost, D("300.00000000"))

        # Every new cost points back at the original lot, with action provenance.
        for lot in result.new_lots:
            self.assertEqual(lot.source_lot_id, "L1")
            self.assertEqual(lot.provenance, ("MERGER",))
            self.assertEqual(lot.account, "ACCT")
            self.assertTrue(lot.lot_id.endswith(f"->{lot.secid}"))

        self.assertEqual(result.unevidenced_lots, ())
        self.assertEqual(result.cash, {})
        # Basis is fully accounted for, exactly once.
        self.assertEqual(
            sum((lot.cost for lot in result.new_lots), D(0)), D("1000.00")
        )

    def testCashInLieu(self):
        actions = [
            LotAction(
                action_id="REORG",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="NEW", ratio=D("0.5"),
                                     cost_fraction=D("0.9")),
                ],
                cash=CashProceeds(amount=D("12.34"), cost_fraction=D("0.1")),
            )
        ]
        result = migrate_lots([make_lot()], actions)
        lots = by_secid(result.new_lots)

        self.assertEqual(len(result.new_lots), 1)
        self.assertEqual(lots["NEW"].units, D("50.00000000"))
        self.assertEqual(lots["NEW"].cost, D("900.00000000"))
        self.assertEqual(result.cash, {"L1": D("12.34000000")})
        # The cash-disposed 10% of basis is not carried into any new lot.
        self.assertEqual(lots["NEW"].cost + D("100.00"), D("1000.00"))

    def testCashOnlyActionLeavesNoLot(self):
        actions = [
            LotAction(
                action_id="CASH_OUT",
                secid="OLD",
                order=0,
                cash=CashProceeds(amount=D("1000.00"), cost_fraction=D(1)),
            )
        ]
        result = migrate_lots([make_lot()], actions)
        self.assertEqual(result.new_lots, ())
        self.assertEqual(result.cash, {"L1": D("1000.00000000")})
        self.assertEqual(result.actions_applied[0].action_id, "CASH_OUT")

    def testCashSplitAcrossLotsByUnits(self):
        actions = [
            LotAction(
                action_id="REORG",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="NEW", ratio=D(1), cost_fraction=D(1)),
                ],
                cash=CashProceeds(amount=D("0.25"), cost_fraction=D(0),
                                   per_unit=True),
            )
        ]
        lots = [
            make_lot(lot_id="L1", units="30"),
            make_lot(lot_id="L2", units="10"),
        ]
        result = migrate_lots(lots, actions)
        # Per-unit rate scales with each lot's size: 0.25 * units.
        self.assertEqual(result.cash["L1"], D("7.50000000"))
        self.assertEqual(result.cash["L2"], D("2.50000000"))

    def testFlatCashAppliesPerLot(self):
        actions = [
            LotAction(
                action_id="REORG",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="NEW", ratio=D(1), cost_fraction=D(1)),
                ],
                cash=CashProceeds(amount=D("10.00"), cost_fraction=D(0)),
            )
        ]
        lots = [
            make_lot(lot_id="L1", units="30"),
            make_lot(lot_id="L2", units="10"),
        ]
        result = migrate_lots(lots, actions)
        # Flat cash attaches once per action application, i.e. once per lot;
        # a rate spanning lot sizes should use CashProceeds(per_unit=True).
        self.assertEqual(result.cash["L1"], D("10.00000000"))
        self.assertEqual(result.cash["L2"], D("10.00000000"))


class UnevidencedTestCase(unittest.TestCase):
    def testAccountWithoutEvidenceIsListedSeparately(self):
        actions = [
            LotAction(
                action_id="MERGER",
                secid="OLD",
                order=0,
                accounts=frozenset({"ACCT_A"}),
                securities=[
                    SecurityProceeds(secid="NEW", ratio=D(1), cost_fraction=D(1))
                ],
            )
        ]
        evidenced = make_lot(lot_id="L1", account="ACCT_A")
        orphan = make_lot(lot_id="L2", account="ACCT_B")
        result = migrate_lots([evidenced, orphan], actions)

        self.assertEqual(len(result.new_lots), 1)
        self.assertEqual(result.new_lots[0].source_lot_id, "L1")
        self.assertEqual(result.unevidenced_lots, (orphan,))
        # The unevidenced lot is passed through untouched.
        self.assertEqual(result.unevidenced_lots[0].units, D("100"))
        self.assertEqual(result.unevidenced_lots[0].cost, D("1000.00"))

    def testSecurityWithoutAnyActionIsUnevidenced(self):
        actions = [
            LotAction(
                action_id="MERGER",
                secid="OTHER",
                order=0,
                securities=[
                    SecurityProceeds(secid="NEW", ratio=D(1), cost_fraction=D(1))
                ],
            )
        ]
        lot = make_lot()
        result = migrate_lots([lot], actions)
        self.assertEqual(result.new_lots, ())
        self.assertEqual(result.unevidenced_lots, (lot,))
        self.assertEqual(result.actions_applied, ())


class OrderedChainTestCase(unittest.TestCase):
    def _actions(self):
        return [
            # Second action on a predecessor the first action empties.
            LotAction(
                action_id="STALE",
                secid="OLD",
                order=1,
                securities=[
                    SecurityProceeds(secid="NEVER", ratio=D(1),
                                     cost_fraction=D(1))
                ],
            ),
            # First action splits OLD into Y (60%) and Z (40%).
            LotAction(
                action_id="STEP1",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="Y", ratio=D("2"),
                                     cost_fraction=D("0.6")),
                    SecurityProceeds(secid="Z", ratio=D("1"),
                                     cost_fraction=D("0.4")),
                ],
            ),
            # Then Z is converted again into Y, converging the branches.
            LotAction(
                action_id="STEP2",
                secid="Z",
                order=0,
                securities=[
                    SecurityProceeds(secid="Y", ratio=D("3"),
                                     cost_fraction=D(1)),
                ],
            ),
        ]

    def testActionsApplyInExplicitOrderWithoutDoubleCounting(self):
        result = migrate_lots([make_lot()], self._actions())
        # The two Y branches merge into a single terminal lot.
        self.assertEqual(len(result.new_lots), 1)
        terminal = result.new_lots[0]
        self.assertEqual(terminal.secid, "Y")
        # 100 OLD * 2 (direct) + 100 OLD * 1 * 3 (via Z)
        self.assertEqual(terminal.units, D("500.00000000"))
        # All of the original basis survives exactly once.
        self.assertEqual(terminal.cost, D("1000.00000000"))
        self.assertEqual(terminal.source_lot_id, "L1")
        self.assertEqual(set(terminal.provenance), {"STEP1", "STEP2"})

        applied_ids = [action.action_id for action in result.actions_applied]
        self.assertEqual(applied_ids, ["STEP1", "STEP2"])
        self.assertNotIn("STALE", applied_ids)

    def testStaleActionSkippedWhenNoBasisRemains(self):
        migrator = LotMigrator(self._actions())
        result = migrator.migrate([make_lot()])
        # STALE targets OLD, but STEP1 already consumed every OLD unit.
        self.assertNotIn(
            "STALE", [action.action_id for action in result.actions_applied]
        )


class RoundingTestCase(unittest.TestCase):
    def testRoundingResidualKeepsBasisIntact(self):
        third = D("1") / D("3")
        actions = [
            LotAction(
                action_id="SPLIT3",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="A", ratio=D(1), cost_fraction=third),
                    SecurityProceeds(secid="B", ratio=D(1), cost_fraction=third),
                    SecurityProceeds(secid="C", ratio=D(1), cost_fraction=third),
                ],
            )
        ]
        result = migrate_lots([make_lot(cost="100.00")], actions)
        self.assertEqual(len(result.new_lots), 3)
        total_cost = sum((lot.cost for lot in result.new_lots), D(0))
        self.assertEqual(total_cost, D("100.00000000"))
        # The final proceed absorbs the 1e-8 rounding residual.
        costs = sorted(lot.cost for lot in result.new_lots)
        self.assertEqual(costs[0], D("33.33333333"))
        self.assertEqual(costs[-1], D("33.33333334"))


class ConvenienceTestCase(unittest.TestCase):
    def testMigrateLotsFunctionAndReusableMigrator(self):
        actions = [
            LotAction(
                action_id="A",
                secid="OLD",
                order=0,
                securities=[
                    SecurityProceeds(secid="NEW", ratio=D(1), cost_fraction=D(1))
                ],
            )
        ]
        migrator = LotMigrator(actions)
        first = migrator.migrate([make_lot(lot_id="L1")])
        second = migrator.migrate([make_lot(lot_id="L2")])
        self.assertEqual(first.new_lots[0].source_lot_id, "L1")
        self.assertEqual(second.new_lots[0].source_lot_id, "L2")

        oneliner = migrate_lots([make_lot(lot_id="L3")], actions)
        self.assertEqual(oneliner.new_lots[0].source_lot_id, "L3")


if __name__ == "__main__":
    unittest.main()
