# Accounting Audit Report

## 1. What Was Checked

Reviewed the current accounting flow across:

- `apps/core`
- `apps/inventory`
- `apps/sales`
- `apps/credits`
- `apps/payables`
- `apps/expenses`
- `apps/reports`
- key templates used by those views
- existing management commands
- Django settings and deploy flow in `build.sh`
- critical test coverage already present in the project

The audit focused on:

- stock and batch integrity
- sale cost and profit integrity
- customer debt creation and repayment
- supplier debt creation and repayment
- cash register consistency
- employee advances, expenses, and salaries
- report consistency against model data

## 2. Current Financial Logic

### Business Logic Map

1. Where a sale is created
- `apps/sales/views.py` in `sale_create`
- `apps/reports/views.py` in `mobile_sale_add`
- model persistence: `apps/sales/models.py`, `Sale` and `SaleItem`

2. Where stock is written off
- `apps/sales/models.py`
- `SaleItem.save()` allocates the selected `PurchaseItem` through `SaleItemBatch`
- stock is synchronized through `sync_store_stock_from_active_inventory()`

3. Where profit is calculated
- `apps/sales/models.py`
- per line: `SaleItem.line_cost_total`, `SaleItem.profit`
- per sale: `Sale.recalculate_totals()`
- aggregated report logic: `apps/reports/services.py`

4. Where cash is calculated
- direct writes:
  - `apps/sales/models.py` for cash sales
  - `apps/credits/models.py` for cash client payments
  - `apps/payables/models.py` for cash supplier payments
  - `apps/expenses/services.py` for advances, store expenses, salaries
- formula / diagnostics:
  - `apps/sales/services.py` in `build_cash_breakdown()` and `recalculate_cash_registers()`

5. Where customer debt is created
- `apps/sales/models.py` in `Sale.sync_credit()`
- only credit sales create or update `Credit`

6. Where client payment is created
- `apps/credits/views.py`
- model logic in `apps/credits/models.py`, `ClientDebtPayment.save()`
- allocations are created FIFO into `CreditPayment`

7. Where client payment is changed or cancelled
- `apps/credits/views.py`
- `ClientDebtPayment.save()` handles edit reallocation
- `ClientDebtPayment.cancel()` handles safe cancellation

8. Where supplier debt is created
- debt is implicit from active `PurchaseItem` rows
- outstanding debt is computed in `apps/payables/models.py`

9. Where supplier payment is created
- `apps/payables/views.py`
- model logic in `apps/payables/models.py`, `SupplierPayment.save()`
- allocations are rebuilt FIFO in `rebuild_supplier_payment_allocations()`

10. Where supplier payment is changed or cancelled
- `apps/payables/views.py`
- `SupplierPayment.save()` handles edits
- `SupplierPayment.cancel()` handles safe cancellation

11. Where reports and dashboard are calculated
- dashboard: `apps/dashboard/views.py`
- daily report / debtors / product profitability: `apps/reports/views.py`
- product profitability services: `apps/reports/services.py`
- expense summaries: `apps/expenses/services.py`
- supplier balances: `apps/payables/views.py`

## 3. Invariants That Must Hold

- stock cannot become negative
- a sale cannot consume more stock than exists
- a sale batch must belong to the same product and store as the sale
- cash sale increases cash, credit sale does not
- customer debt equals credit sales minus active payments
- cancelled payments must not reduce debt
- supplier debt equals active purchases minus active supplier payment allocations
- supplier overpayment must be blocked
- all money and quantity calculations must use `Decimal`
- all critical write operations must be inside `transaction.atomic()`

## 4. Errors Found

### Confirmed Real Bugs

1. Non-cash client payments were affecting cash
- `ClientDebtPayment.save()` used to increase cash for every active payment regardless of `payment_method`
- this could overstate the cash register after card or transfer receipts

2. Non-cash supplier payments were affecting cash
- `SupplierPayment.save()` used to decrease cash and validate cash balance for every payment regardless of `payment_method`
- this could understate the cash register and incorrectly block bank-transfer supplier payments

3. Cash formula counted non-cash client and supplier payments as cash movements
- `apps/sales/services.py` in `build_cash_breakdown()`
- this could make diagnostics, dashboard cash checks, and repair commands inconsistent

### Structural Risks Not Auto-Rewritten

1. There are legacy duplicate method definitions left in some model files
- especially `apps/inventory/models.py` and `apps/sales/models.py`
- Python uses the later definitions, so runtime works, but this increases maintenance risk

2. Some report logic is duplicated between view-level queries and service-level queries
- this increases the chance of future divergence

3. Store expenses, salaries, and employee advances currently have no payment method field
- by current implementation they are always treated as cash operations
- this is consistent with current code, but it is a product limitation if non-cash expense flows are needed later

## 5. Errors Fixed

Fixed without changing the interface:

- client payments now affect the cash register only when `payment_method == cash`
- supplier payments now affect the cash register only when `payment_method == cash`
- non-cash supplier payments no longer require available cash in the store cash register
- cash diagnostics now count only cash client payments and cash supplier payments
- a new read-only integrity audit command was added:
  - `python manage.py audit_accounting_integrity`

## 6. Tests Added or Expanded

Added / expanded tests for:

- non-cash client payment does not change cash
- non-cash supplier payment does not change cash and can work without available cash balance
- cash breakdown ignores non-cash client and supplier payments
- daily report totals for sales, cost, expenses, salary, net profit, and credit debt
- audit command returns `0` for consistent data
- audit command returns `1` and reports a problem for broken client payment allocations

Existing critical tests already covered and were kept green for:

- selected purchase batch sale allocation
- oversell prevention
- credit sale debt creation
- partial / full / cancelled / updated client payments
- supplier FIFO allocations
- supplier payment cancel / update
- purchase quantity and price changes
- store expense / salary / advance flows

## 7. Remaining Potential Risks

- legacy inconsistent production data can still exist even if current code is correct
- old soft-deleted records remain in tables by design and must always be filtered correctly
- the project still contains some garbled Cyrillic strings in older files and templates; this is mostly a maintainability issue, not a calculation bug
- full physical cash correctness still depends on users not making manual DB edits outside the application

## 8. Useful Verification Commands

```bash
python manage.py check
python manage.py test
python manage.py audit_accounting_integrity
```

Additional read-only / repair utilities already present:

```bash
python manage.py diagnose_cash
python manage.py diagnose_fifo
python manage.py repair_client_payment_allocations
python manage.py repair_supplier_payment_allocations
```

## 9. Data Discrepancies

On the current local database at the time of this audit:

- `audit_accounting_integrity` returned `CRITICAL: 0`
- no automatic data repair was run as part of this audit
- the command is read-only by design

If production data later shows inconsistencies:

- diagnose first with `audit_accounting_integrity`
- repair only with an explicit repair command or a one-off approved migration

## 10. Manual Checks Recommended on the Site

1. Create a client payment with `Перевод` or `Карта`
- verify customer debt decreases
- verify cash register does not increase

2. Create a supplier payment with `Перевод`
- verify supplier debt decreases
- verify cash register does not decrease

3. Open:
- dashboard
- cash page
- daily report
- debtors list
- supplier balances

Verify that totals remain consistent after the two scenarios above.
