from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce

from apps.core.models import Store
from apps.credits.models import ClientDebtPayment, Credit, CreditPayment
from apps.credits.services import build_debtor_rows
from apps.expenses.models import EmployeeAdvance, Expense, SalaryPayment, StoreExpense
from apps.inventory.models import PurchaseItem, StoreStock
from apps.payables.models import SupplierOverpayment, SupplierPayment, SupplierPaymentAllocation
from apps.payables.services import simulate_supplier_settlement
from apps.reports.services import build_product_profitability_rows
from apps.sales.models import Sale, SaleItem, SaleItemBatch
from apps.sales.services import build_cash_breakdown


ZERO_MONEY = Decimal("0.00")
ZERO_QTY = Decimal("0.000")
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
QTY_FIELD = DecimalField(max_digits=14, decimal_places=3)


def _money(value):
    return (value or ZERO_MONEY).quantize(Decimal("0.01"))


def _qty(value):
    return (value or ZERO_QTY).quantize(Decimal("0.001"))


def _new_section(name):
    return {"name": name, "critical": [], "warning": [], "info": []}


def _append(section, level, message):
    section[level].append(message)


def _sum_decimal(queryset, field_name, *, output_field=MONEY_FIELD):
    return queryset.aggregate(
        total=Coalesce(Sum(field_name), Value(Decimal("0.00"), output_field=output_field))
    )["total"]


def _purchase_totals_by_store():
    line_total = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )
    rows = (
        PurchaseItem.objects.filter(purchase__deleted_at__isnull=True)
        .annotate(line_total=line_total)
        .values(
            "purchase_id",
            "purchase__date",
            "purchase__supplier_id",
            "purchase__supplier__name",
            "store_id",
            "store__name",
        )
        .annotate(total=Coalesce(Sum("line_total"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))
    )
    return {
        (row["purchase_id"], row["store_id"]): {
            "purchase_id": row["purchase_id"],
            "purchase_date": row["purchase__date"],
            "supplier_id": row["purchase__supplier_id"],
            "supplier_name": row["purchase__supplier__name"],
            "store_id": row["store_id"],
            "store_name": row["store__name"],
            "total": _money(row["total"]),
        }
        for row in rows
    }


def _active_batch_quantity_by_sale_item():
    return {
        row["sale_item_id"]: _qty(row["total"])
        for row in SaleItemBatch.objects.filter(
            sale_item__sale__deleted_at__isnull=True,
            purchase_item__purchase__deleted_at__isnull=True,
        )
        .values("sale_item_id")
        .annotate(total=Coalesce(Sum("quantity"), Value(ZERO_QTY, output_field=QTY_FIELD)))
    }


def _active_batch_quantity_by_purchase_item():
    return {
        row["purchase_item_id"]: _qty(row["total"])
        for row in SaleItemBatch.objects.filter(
            sale_item__sale__deleted_at__isnull=True,
            purchase_item__purchase__deleted_at__isnull=True,
        )
        .values("purchase_item_id")
        .annotate(total=Coalesce(Sum("quantity"), Value(ZERO_QTY, output_field=QTY_FIELD)))
    }


def _active_batch_cost_by_sale_item():
    total_cost_expr = ExpressionWrapper(
        F("quantity") * F("purchase_item__purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )
    return {
        row["sale_item_id"]: _money(row["total_cost"])
        for row in SaleItemBatch.objects.filter(
            sale_item__sale__deleted_at__isnull=True,
            purchase_item__purchase__deleted_at__isnull=True,
        )
        .annotate(total_cost_line=total_cost_expr)
        .values("sale_item_id")
        .annotate(total_cost=Coalesce(Sum("total_cost_line"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))
    }


def run_full_accounting_reconciliation():
    sections = [
        _audit_sales(),
        _audit_inventory(),
        _audit_supplier_payments(),
        _audit_supplier_debts(),
        _audit_client_payments(),
        _audit_client_debts(),
        _audit_cash(),
        _audit_reports(),
    ]
    summary = {
        "critical": sum(len(section["critical"]) for section in sections),
        "warning": sum(len(section["warning"]) for section in sections),
        "info": sum(len(section["info"]) for section in sections),
    }
    return {
        "sections": sections,
        "summary": summary,
        "has_critical": summary["critical"] > 0,
    }


def _audit_sales():
    section = _new_section("Sales")

    active_sales = Sale.objects.select_related("store", "customer").filter(deleted_at__isnull=True)
    item_counts = {
        row["sale_id"]: row["count"]
        for row in SaleItem.objects.filter(sale__deleted_at__isnull=True)
        .values("sale_id")
        .annotate(count=Count("id"))
    }
    item_sums = {
        row["sale_id"]: {
            "total": _money(row["total"]),
            "cost": _money(row["cost"]),
            "profit": _money(row["profit"]),
        }
        for row in SaleItem.objects.filter(sale__deleted_at__isnull=True)
        .values("sale_id")
        .annotate(
            total=Coalesce(Sum("line_total"), Value(ZERO_MONEY, output_field=MONEY_FIELD)),
            cost=Coalesce(Sum("line_cost_total"), Value(ZERO_MONEY, output_field=MONEY_FIELD)),
            profit=Coalesce(Sum("profit"), Value(ZERO_MONEY, output_field=MONEY_FIELD)),
        )
    }

    for sale in active_sales:
        if item_counts.get(sale.id, 0) <= 0:
            _append(section, "critical", f"Sale #{sale.id} has no SaleItem rows.")
        totals = item_sums.get(sale.id, {"total": ZERO_MONEY, "cost": ZERO_MONEY, "profit": ZERO_MONEY})
        if _money(sale.total_amount) != totals["total"]:
            _append(section, "critical", f"Sale #{sale.id} total mismatch: stored={sale.total_amount}, items={totals['total']}.")
        if _money(sale.total_cost) != totals["cost"]:
            _append(section, "critical", f"Sale #{sale.id} cost mismatch: stored={sale.total_cost}, items={totals['cost']}.")
        if _money(sale.total_profit) != totals["profit"]:
            _append(section, "critical", f"Sale #{sale.id} profit mismatch: stored={sale.total_profit}, items={totals['profit']}.")
        if sale.payment_type == Sale.PAYMENT_TYPE_CREDIT and not hasattr(sale, "credit"):
            _append(section, "critical", f"Credit sale #{sale.id} has no Credit debt row.")
        if sale.payment_type == Sale.PAYMENT_TYPE_CASH and hasattr(sale, "credit"):
            _append(section, "critical", f"Cash sale #{sale.id} unexpectedly has Credit debt row.")

    batch_quantity_by_item = _active_batch_quantity_by_sale_item()
    batch_cost_by_item = _active_batch_cost_by_sale_item()
    for item in SaleItem.objects.select_related("sale", "product").filter(sale__deleted_at__isnull=True):
        allocated_qty = batch_quantity_by_item.get(item.id, ZERO_QTY)
        if allocated_qty != _qty(item.quantity_kg):
            _append(section, "critical", f"SaleItem #{item.id} allocation quantity mismatch: sale={item.quantity_kg}, allocations={allocated_qty}.")
        expected_cost = batch_cost_by_item.get(item.id, ZERO_MONEY)
        if _money(item.line_cost_total) != expected_cost:
            _append(section, "critical", f"SaleItem #{item.id} cost mismatch: stored={item.line_cost_total}, batches={expected_cost}.")
        expected_profit = _money(item.line_total - item.line_cost_total)
        if _money(item.profit) != expected_profit:
            _append(section, "critical", f"SaleItem #{item.id} profit mismatch: stored={item.profit}, expected={expected_profit}.")

    if not section["critical"] and not section["warning"]:
        _append(section, "info", f"Sales OK: active_sales={active_sales.count()}, active_sale_items={SaleItem.objects.filter(sale__deleted_at__isnull=True).count()}.")
    return section


def _audit_inventory():
    section = _new_section("Inventory")

    allocated_by_purchase_item = _active_batch_quantity_by_purchase_item()
    stock_by_pair = defaultdict(lambda: ZERO_QTY)

    for batch in SaleItemBatch.objects.select_related(
        "sale_item__sale",
        "sale_item__product",
        "purchase_item__purchase",
        "purchase_item__store",
        "purchase_item__product",
    ):
        if batch.quantity <= ZERO_QTY:
            _append(section, "critical", f"SaleItemBatch #{batch.id} has non-positive quantity {batch.quantity}.")
        if batch.sale_item.sale.deleted_at or batch.purchase_item.purchase.deleted_at:
            continue
        if batch.quantity > batch.purchase_item.quantity_kg:
            _append(section, "critical", f"SaleItemBatch #{batch.id} quantity {batch.quantity} exceeds purchase item quantity {batch.purchase_item.quantity_kg}.")
        if batch.sale_item.product_id != batch.purchase_item.product_id:
            _append(section, "critical", f"SaleItemBatch #{batch.id} product mismatch.")
        if batch.sale_item.sale.store_id != batch.purchase_item.store_id:
            _append(section, "critical", f"SaleItemBatch #{batch.id} store mismatch.")

    for item in PurchaseItem.objects.select_related("purchase", "store", "product").filter(purchase__deleted_at__isnull=True):
        sold_qty = allocated_by_purchase_item.get(item.id, ZERO_QTY)
        remaining_qty = _qty(item.quantity_kg - sold_qty)
        if sold_qty > _qty(item.quantity_kg):
            _append(section, "critical", f"PurchaseItem #{item.id} oversold: purchased={item.quantity_kg}, sold_by_allocations={sold_qty}.")
        if remaining_qty < ZERO_QTY:
            _append(section, "critical", f"PurchaseItem #{item.id} negative remaining quantity {remaining_qty}.")
        stock_by_pair[(item.store_id, item.product_id)] += remaining_qty

    for stock in StoreStock.objects.select_related("store", "product"):
        expected = _qty(stock_by_pair.get((stock.store_id, stock.product_id), ZERO_QTY))
        if _qty(stock.quantity_kg) != expected:
            _append(section, "critical", f"Stock mismatch for store={stock.store.name}, product={stock.product.name}: stored={stock.quantity_kg}, batch_sum={expected}.")
        if stock.quantity_kg < ZERO_QTY:
            _append(section, "critical", f"Stock #{stock.id} is negative: {stock.quantity_kg}.")

    for (store_id, product_id), expected in stock_by_pair.items():
        if not StoreStock.objects.filter(store_id=store_id, product_id=product_id).exists() and expected != ZERO_QTY:
            _append(section, "critical", f"Missing StoreStock row for store_id={store_id}, product_id={product_id}, expected={expected}.")

    if not section["critical"] and not section["warning"]:
        _append(section, "info", "Inventory OK: StoreStock matches sum of active purchase batches minus active allocations.")
    return section


def _audit_supplier_payments():
    section = _new_section("Supplier Payments")

    active_cash_total = _money(
        _sum_decimal(
            SupplierPayment.objects.filter(
                status=SupplierPayment.STATUS_ACTIVE,
                payment_method=SupplierPayment.PAYMENT_METHOD_CASH,
            ),
            "amount",
        )
    )
    cancelled_cash_total = _money(
        _sum_decimal(
            SupplierPayment.objects.filter(
                status=SupplierPayment.STATUS_CANCELLED,
                payment_method=SupplierPayment.PAYMENT_METHOD_CASH,
            ),
            "amount",
        )
    )
    active_non_cash_total = _money(
        _sum_decimal(
            SupplierPayment.objects.filter(status=SupplierPayment.STATUS_ACTIVE).exclude(
                payment_method=SupplierPayment.PAYMENT_METHOD_CASH
            ),
            "amount",
        )
    )
    _append(section, "info", f"Active cash supplier payments: {active_cash_total}.")
    _append(section, "info", f"Cancelled cash supplier payments ignored by cash formula: {cancelled_cash_total}.")
    _append(section, "info", f"Active non-cash supplier payments: {active_non_cash_total}.")

    for payment in SupplierPayment.objects.select_related("supplier", "store").all():
        active_allocation_total = _money(
            payment.allocations.filter(payment__status=SupplierPayment.STATUS_ACTIVE).aggregate(
                total=Coalesce(Sum("amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD))
            )["total"]
        )
        all_allocation_total = _money(
            payment.allocations.aggregate(
                total=Coalesce(Sum("amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD))
            )["total"]
        )
        try:
            overpayment_total = _money(payment.overpayment.remaining_amount)
        except SupplierOverpayment.DoesNotExist:
            overpayment_total = ZERO_MONEY

        if payment.status == SupplierPayment.STATUS_ACTIVE:
            covered = _money(active_allocation_total + overpayment_total)
            if covered != _money(payment.amount):
                _append(section, "warning", f"Active supplier payment #{payment.id} coverage differs from amount: amount={payment.amount}, allocations+overpayment={covered}.")
        else:
            if active_allocation_total != ZERO_MONEY:
                _append(section, "critical", f"Cancelled supplier payment #{payment.id} still has active allocation effect {active_allocation_total}.")
            if all_allocation_total:
                _append(section, "info", f"Cancelled supplier payment #{payment.id} keeps historical allocations={all_allocation_total}, ignored in active debt.")

    purchase_totals = _purchase_totals_by_store()
    allocated_by_purchase = defaultdict(lambda: ZERO_MONEY)
    for allocation in SupplierPaymentAllocation.objects.select_related("payment", "purchase").filter(
        payment__status=SupplierPayment.STATUS_ACTIVE,
        purchase__deleted_at__isnull=True,
    ):
        key = (allocation.purchase_id, allocation.store_id)
        allocated_by_purchase[key] += allocation.amount
        total = purchase_totals.get(key, {}).get("total", ZERO_MONEY)
        if allocation.amount > total and total != ZERO_MONEY:
            _append(section, "critical", f"Supplier allocation #{allocation.id} amount {allocation.amount} exceeds purchase total {total}.")

    for key, allocated in allocated_by_purchase.items():
        total = purchase_totals.get(key, {}).get("total", ZERO_MONEY)
        if _money(allocated) > total:
            _append(section, "critical", f"Purchase #{key[0]} store #{key[1]} active allocations exceed purchase total: allocated={_money(allocated)}, total={total}.")

    return section


def _audit_supplier_debts():
    section = _new_section("Supplier Debts")
    purchase_totals = _purchase_totals_by_store()
    allocated_by_purchase = defaultdict(lambda: ZERO_MONEY)
    for allocation in SupplierPaymentAllocation.objects.filter(
        payment__status=SupplierPayment.STATUS_ACTIVE,
        purchase__deleted_at__isnull=True,
    ):
        allocated_by_purchase[(allocation.purchase_id, allocation.store_id)] += allocation.amount

    total_purchase_amount = ZERO_MONEY
    total_allocated_amount = ZERO_MONEY
    total_debt_amount = ZERO_MONEY
    groups = set()
    for key, row in purchase_totals.items():
        paid = _money(allocated_by_purchase[key])
        debt = _money(row["total"] - paid)
        if debt < ZERO_MONEY:
            _append(section, "critical", f"Purchase #{key[0]} store #{key[1]} has negative supplier debt {debt}.")
            debt = ZERO_MONEY
        total_purchase_amount += row["total"]
        total_allocated_amount += paid
        total_debt_amount += debt
        groups.add((row["supplier_id"], row["store_id"], row["supplier_name"], row["store_name"]))

    for supplier_id, store_id, supplier_name, store_name in sorted(groups, key=lambda item: (item[3], item[2])):
        simulation = simulate_supplier_settlement(supplier_id=supplier_id, store_id=store_id)
        summary = simulation["summary"]
        _append(
            section,
            "info",
            (
                f"Supplier/store debt {supplier_name} / {store_name}: "
                f"purchases={summary['total_purchase_amount']}, active_paid={summary['total_paid_amount']}, "
                f"debt={summary['total_due_amount']}, overpayment={summary['total_overpayment']}."
            ),
        )
        if store_name == "Алик":
            _append(section, "info", f"ALIK supplier focus: {supplier_name}, debt={summary['total_due_amount']}.")

    _append(
        section,
        "info",
        f"Supplier debt totals: purchases={_money(total_purchase_amount)}, active_allocations={_money(total_allocated_amount)}, debt={_money(total_debt_amount)}.",
    )
    return section


def _audit_client_payments():
    section = _new_section("Client Payments")
    active_cash_total = _money(
        _sum_decimal(
            ClientDebtPayment.objects.filter(
                status=ClientDebtPayment.STATUS_ACTIVE,
                payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            ),
            "amount",
        )
    )
    cancelled_cash_total = _money(
        _sum_decimal(
            ClientDebtPayment.objects.filter(
                status=ClientDebtPayment.STATUS_CANCELLED,
                payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            ),
            "amount",
        )
    )
    _append(section, "info", f"Active cash client payments: {active_cash_total}.")
    _append(section, "info", f"Cancelled cash client payments ignored by cash formula: {cancelled_cash_total}.")

    for payment in ClientDebtPayment.objects.select_related("store", "client").all():
        all_allocation_total = _money(payment.allocations.aggregate(total=Coalesce(Sum("amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))["total"])
        active_allocation_total = _money(
            payment.allocations.filter(client_debt_payment__status=ClientDebtPayment.STATUS_ACTIVE).aggregate(
                total=Coalesce(Sum("amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD))
            )["total"]
        )
        if payment.status == ClientDebtPayment.STATUS_ACTIVE:
            if active_allocation_total != _money(payment.amount):
                _append(section, "warning", f"Active client payment #{payment.id} allocations differ from amount: amount={payment.amount}, allocations={active_allocation_total}.")
        else:
            if active_allocation_total != ZERO_MONEY:
                _append(section, "critical", f"Cancelled client payment #{payment.id} still has active allocation effect {active_allocation_total}.")
            if all_allocation_total:
                _append(section, "info", f"Cancelled client payment #{payment.id} keeps historical allocations={all_allocation_total}, ignored in active debt.")
    return section


def _audit_client_debts():
    section = _new_section("Client Debts")
    credit_sales_missing_debt = Sale.objects.filter(
        deleted_at__isnull=True,
        payment_type=Sale.PAYMENT_TYPE_CREDIT,
        credit__isnull=True,
    )
    for sale in credit_sales_missing_debt:
        _append(section, "critical", f"Credit sale #{sale.id} has no Credit row.")

    cash_sales_with_debt = Sale.objects.filter(
        deleted_at__isnull=True,
        payment_type=Sale.PAYMENT_TYPE_CASH,
        credit__isnull=False,
    )
    for sale in cash_sales_with_debt:
        _append(section, "critical", f"Cash sale #{sale.id} has unexpected Credit row.")

    debtor_rows = build_debtor_rows()
    debtor_report_total = _money(sum((row["total_debt"] for row in debtor_rows), ZERO_MONEY))
    credit_model_total = _money(
        Credit.objects.filter(sale__deleted_at__isnull=True)
        .exclude(status=Credit.STATUS_PAID)
        .aggregate(total=Coalesce(Sum("remaining_amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))["total"]
    )
    if debtor_report_total != credit_model_total:
        _append(section, "critical", f"Client debtor list mismatch: report={debtor_report_total}, model={credit_model_total}.")

    for credit in Credit.objects.select_related("store", "customer", "sale").filter(sale__deleted_at__isnull=True):
        active_paid = _money(
            CreditPayment.objects.filter(
                credit=credit,
                client_debt_payment__status=ClientDebtPayment.STATUS_ACTIVE,
            ).aggregate(total=Coalesce(Sum("amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))["total"]
        )
        legacy_paid = _money(
            CreditPayment.objects.filter(
                credit=credit,
                client_debt_payment__isnull=True,
            ).aggregate(total=Coalesce(Sum("amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))["total"]
        )
        expected_remaining = _money(credit.original_amount - active_paid - legacy_paid)
        if expected_remaining < ZERO_MONEY:
            _append(section, "critical", f"Credit #{credit.id} is overpaid: original={credit.original_amount}, paid={_money(active_paid + legacy_paid)}.")
            expected_remaining = ZERO_MONEY
        if _money(credit.remaining_amount) != expected_remaining:
            _append(section, "critical", f"Credit #{credit.id} remaining mismatch: stored={credit.remaining_amount}, expected={expected_remaining}.")

    _append(section, "info", f"Client debt total: {debtor_report_total}.")
    return section


def _audit_cash():
    section = _new_section("Cash")
    for store in Store.objects.order_by("name"):
        breakdown = build_cash_breakdown(store)
        cancelled_supplier_cash = _money(
            _sum_decimal(
                SupplierPayment.objects.filter(
                    store=store,
                    status=SupplierPayment.STATUS_CANCELLED,
                    payment_method=SupplierPayment.PAYMENT_METHOD_CASH,
                ),
                "amount",
            )
        )
        cancelled_client_cash = _money(
            _sum_decimal(
                ClientDebtPayment.objects.filter(
                    store=store,
                    status=ClientDebtPayment.STATUS_CANCELLED,
                    payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
                ),
                "amount",
            )
        )
        message = (
            f"Cash store={store.name}: stored={breakdown['stored_balance']}, formula={breakdown['formula_balance']}, "
            f"delta={breakdown['difference']}, cash_sales={breakdown['cash_sales']}, "
            f"cash_client_payments={breakdown['client_debt_payments']}, active_cash_supplier_payments={breakdown['supplier_payments']}, "
            f"cancelled_cash_supplier_payments={cancelled_supplier_cash}, store_expenses={breakdown['store_expenses']}, "
            f"employee_advances={breakdown['employee_advances']}, salaries={breakdown['salary_payments']}, "
            f"legacy_credit_payments={breakdown['legacy_credit_payments']}, cancelled_cash_client_payments={cancelled_client_cash}, "
            "other_cash_movements=0.00."
        )
        if breakdown["difference"] != ZERO_MONEY:
            _append(section, "critical", message)
        else:
            _append(section, "info", message)
        if store.name == "Алик":
            _append(section, "info", f"ALIK CASH FOCUS: {message}")
    return section


def _audit_reports():
    section = _new_section("Reports")

    rows = build_product_profitability_rows(group_by_store=True)
    report_revenue = _money(sum((row["revenue"] for row in rows), ZERO_MONEY))
    report_cost = _money(sum((row["sold_cost"] for row in rows), ZERO_MONEY))
    sale_revenue = _money(
        Sale.objects.filter(deleted_at__isnull=True).aggregate(
            total=Coalesce(Sum("total_amount"), Value(ZERO_MONEY, output_field=MONEY_FIELD))
        )["total"]
    )
    batch_cost_expr = ExpressionWrapper(
        F("quantity") * F("purchase_item__purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )
    batch_cost = _money(
        SaleItemBatch.objects.filter(
            sale_item__sale__deleted_at__isnull=True,
            purchase_item__purchase__deleted_at__isnull=True,
        )
        .annotate(cost_line=batch_cost_expr)
        .aggregate(total=Coalesce(Sum("cost_line"), Value(ZERO_MONEY, output_field=MONEY_FIELD)))["total"]
    )
    if report_revenue != sale_revenue:
        _append(section, "critical", f"Product profitability revenue mismatch: report={report_revenue}, sales={sale_revenue}.")
    if report_cost != batch_cost:
        _append(section, "critical", f"Product profitability cost mismatch: report={report_cost}, batches={batch_cost}.")

    debtor_rows = build_debtor_rows()
    _append(section, "info", f"Reports baseline: product_rows={len(rows)}, debtor_rows={len(debtor_rows)}, revenue={report_revenue}, cost={report_cost}.")
    if not section["critical"] and not section["warning"]:
        _append(section, "info", "Reports OK.")
    return section
