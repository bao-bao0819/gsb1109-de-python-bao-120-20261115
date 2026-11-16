.. _lots:

Cost-Basis Lot Migration
========================

``ofxtools.lots`` migrates investment cost-basis lots through broker
corporate-action worksheets (mergers, splits, spin-offs, redemptions).  An
action states, for each old security, how many units of each new security one
old unit converts into, any cash received in lieu of fractional units, and how
the original cost basis is apportioned among the proceeds.

Every new lot back-references the lot whose basis it inherited
(``source_lot_id``) and records the ordered chain of action IDs the basis
flowed through (``provenance``).  Basis is allocated strictly according to the
action's ``cost_fraction`` values, so the same basis can never be apportioned
twice.  Lots whose account has no applicable action are returned untouched in a
separate ``unevidenced_lots`` collection rather than being migrated.

.. code-block:: python

    from decimal import Decimal
    from ofxtools.lots import (
        CashProceeds,
        Lot,
        LotAction,
        SecurityProceeds,
        migrate_lots,
    )

    lot = Lot(
        lot_id="L1",
        secid="OLD",
        units=Decimal("100"),
        cost=Decimal("1000.00"),
        account="ACCT",
    )

    action = LotAction(
        action_id="MERGER",
        secid="OLD",
        order=0,
        securities=[
            # 0.5 new units per old unit; 70% of the basis
            SecurityProceeds(
                secid="NEWA",
                ratio=Decimal("0.5"),
                cost_fraction=Decimal("0.7"),
            ),
            # 0.25 new units per old unit; 30% of the basis
            SecurityProceeds(
                secid="NEWB",
                ratio=Decimal("0.25"),
                cost_fraction=Decimal("0.3"),
            ),
        ],
        # Optional cash in lieu of the fractional remainder.  ``per_unit``
        # makes the amount a per-unit rate so one rule spans many lots.
        cash=CashProceeds(
            amount=Decimal("0.10"),
            cost_fraction=Decimal("0"),
            per_unit=True,
        ),
    )

    result = migrate_lots([lot], [action])

    for new_lot in result.new_lots:
        print(
            new_lot.secid,
            new_lot.units,
            new_lot.cost,
            new_lot.source_lot_id,   # 'L1'
            new_lot.provenance,      # ('MERGER',)
        )

    print(result.cash)              # {'L1': Decimal('10.00000000')}
    print(result.unevidenced_lots)  # ()

Chained actions sharing a predecessor are applied in explicit ``order``; an
action only ever consumes basis still held in its predecessor security, so
multi-step reorganizations (e.g. ``OLD -> Y, Z`` then ``Z -> Y``) converge
without double counting.  Restrict an action to specific accounts with
``accounts=frozenset({...})``; lots in any other account lack evidence and are
listed separately.
