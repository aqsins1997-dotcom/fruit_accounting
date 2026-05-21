from collections import defaultdict
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.models import Store
from apps.credits.models import ClientDebtPayment, Credit, CreditPayment
from apps.credits.services import build_debtor_rows
from apps.expenses.models import EmployeeAdvance, Expense, SalaryPayment, StoreExpense
from apps.inventory.models import PurchaseItem, StoreStock, calculate_active_stock_quantity
from apps.payables.models import SupplierOverpayment, SupplierPayment, SupplierPaymentAllocation
from apps.reports.services import build_product_profitability_rows
from apps.sales.models import Sale, SaleItem, SaleItemBatch
from apps.sales.services import build_cash_breakdown

ZERO_MONEY = Decimal("0.00")
ZERO_QTY = Decimal("0.000")
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


def _money(value):
    return (value or ZERO_MONEY).quantize(Decimal("0.01"))


def _qty(value):
    return (value or ZERO_QTY).quantize(Decimal("0.001"))


def _new_section(name):
    return {"name": name, "critical": [], "warning": [], "info": []}


def _append(section, level, message):
    section[level].append(message)


def _supplier_purchase_totals():
    line_total = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )
    rows = (
        PurchaseItem.objects.filter(purchase__deleted_at__isnull=True)
        .annotate(line_total=line_total)
        .values("purchase_id", "store_id", "purchase__supplier_id", "purchase__date")
        .annotate(
            purchase_total=Coalesce(
                Sum("line_total"),
                Value(ZERO_MONEY, output_field=MONEY_FIELD),
            )
        )
    )
    return {
        (row["purchase_id"], row["store_id"]): {
            "supplier_id": row["purchase__supplier_id"],
            "date": row["purchase__date"],
            "total": _money(row["purchase_total"]),
        }
        for row in rows
    }


def run_accounting_audit():
    sections = []

    inventory = _audit_inventory()
    sections.append(inventory)

    clients = _audit_clients()
    sections.append(clients)

    suppliers = _audit_suppliers()
    sections.append(suppliers)

    cash = _audit_cash()
    sections.append(cash)

    reports = _audit_reports()
    sections.append(reports)

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


def _audit_inventory():
    section = _new_section("Inventory")

    batch_totals = {
        row["sale_item_id"]: _qty(row["total_qty"])
        for row in SaleItemBatch.objects.filter(
            sale_item__sale__deleted_at__isnull=True,
            purchase_item__purchase__deleted_at__isnull=True,
        )
        .values("sale_item_id")
        .annotate(total_qty=Coalesce(Sum("quantity"), Value(ZERO_QTY)))
    }

    for sale_item in SaleItem.objects.select_related("sale", "product").filter(sale__deleted_at__isnull=True):
        batch_quantity = batch_totals.get(sale_item.id, ZERO_QTY)
        if batch_quantity <= ZERO_QTY:
            _append(
                section,
                "critical",
                f"SaleItem #{sale_item.id} has no active batch allocation.",
            )
        elif batch_quantity != _qty(sale_item.quantity_kg):
            _append(
                section,
                "critical",
                f"SaleItem #{sale_item.id} batch quantity {batch_quantity} does not match sale quantity {sale_item.quantity_kg}.",
            )

    for batch in SaleItemBatch.objects.select_related("sale_item__sale", "purchase_item__purchase", "purchase_item", "sale_item__product"):
        if batch.sale_item.sale.deleted_at or batch.purchase_item.purchase.deleted_at:
            continue
        if batch.purchase_item.product_id != batch.sale_item.product_id:
            _append(
                section,
                "critical",
                f"SaleItemBatch #{batch.id} links sale product #{batch.sale_item.product_id} to purchase product #{batch.purchase_item.product_id}.",
            )
        if batch.purchase_item.store_id != batch.sale_item.sale.store_id:
            _append(
                section,
                "critical",
                f"SaleItemBatch #{batch.id} links sale store #{batch.sale_item.sale.store_id} to purchase store #{batch.purchase_item.store_id}.",
            )

    for purchase_item in PurchaseItem.objects.select_related("purchase", "store", "product").filter(
        purchase__deleted_at__isnull=True
    ):
        allocated = (
            SaleItemBatch.objects.filter(
                purchase_item=purchase_item,
                sale_item__sale__deleted_at__isnull=True,
                purchase_item__purchase__deleted_at__isnull=True,
            ).aggregate(total=Coalesce(Sum("quantity"), Value(ZERO_QTY)))["total"]
            or ZERO_QTY
        )
        allocated = _qty(allocated)
        remaining = _qty(purchase_item.quantity_kg - allocated)
        if allocated > _qty(purchase_item.quantity_kg):
            _append(
                section,
                "critical",
                f"PurchaseItem #{purchase_item.id} oversold: purchased={purchase_item.quantity_kg}, allocated={allocated}.",
            )
        if remaining < ZERO_QTY:
            _append(
                section,
                "critical",
                f"PurchaseItem #{purchase_item.id} has negative remaining quantity {remaining}.",
            )

    for stock in StoreStock.objects.select_related("store", "product").all():
        expected_quantity = calculate_active_stock_quantity(
            store_id=stock.store_id,
            product_id=stock.product_id,
        )
        if stock.quantity_kg < ZERO_QTY:
            _append(
                section,
                "critical",
                f"StoreStock #{stock.id} is negative: {stock.quantity_kg}.",
            )
        if _qty(stock.quantity_kg) != _qty(expected_quantity):
            _append(
                section,
                "critical",
                f"StoreStock #{stock.id} mismatch for store={stock.store.name}, product={stock.product.name}: stored={stock.quantity_kg}, expected={expected_quantity}.",
            )

    if not any(section[level] for level in ("critical", "warning")):
        _append(section, "info", "No inventory integrity problems found.")
    return section


def _audit_clients():
    section = _new_section("Clients")

    active_payments = ClientDebtPayment.objects.select_related("store", "client").filter(
        status=ClientDebtPayment.STATUS_ACTIVE
    )
    for payment in active_payments:
        allocations = payment.allocations.select_related("credit__sale")
        allocation_total = _money(
            allocations.aggregate(total=Coalesce(Sum("amount"), Value(ZERO_MONEY)))["total"]
        )
        if not allocations.exists():
            _append(
                section,
                "critical",
                f"Active client payment #{payment.id} ({payment.client.name}, {payment.amount}) has no allocations.",
            )
        if allocation_total > _money(payment.amount):
            _append(
                section,
                "critical",
                f"Client payment #{payment.id} allocations {allocation_total} exceed payment amount {payment.amount}.",
            )
        if allocation_total != _money(payment.amount):
            _append(
                section,
                "warning",
                f"Client payment #{payment.id} allocations {allocation_total} differ from payment amount {payment.amount}.",
            )

    cancelled_payments = ClientDebtPayment.objects.select_related("store", "client").filter(
        status=ClientDebtPayment.STATUS_CANCELLED
    )
    for payment in cancelled_payments:
        if payment.allocations.exists():
            _append(
                section,
                "warning",
                f"Cancelled client payment #{payment.id} still has allocation rows.",
            )

    for credit in Credit.objects.select_related("customer", "store", "sale").filter(
        sale__deleted_at__isnull=True
    ):
        expected_paid = sum(
            (
                allocation.amount
                for allocation in credit.payments.filter(
                    client_debt_payment__isnull=True
                )
            ),
            ZERO_MONEY,
        )
        expected_paid += sum(
            (
                allocation.amount
                for allocation in credit.payments.filter(
                    client_debt_payment__status=ClientDebtPayment.STATUS_ACTIVE
                )
            ),
            ZERO_MONEY,
        )
        expected_paid = _money(expected_paid)
        expected_remaining = _money(credit.original_amount - expected_paid)
        if expected_remaining < ZERO_MONEY:
            _append(
                section,
                "critical",
                f"Credit #{credit.id} has overpayment: original={credit.original_amount}, paid={expected_paid}.",
            )
            expected_remaining = ZERO_MONEY

        expected_status = (
            Credit.STATUS_UNPAID
            if expected_remaining == _money(credit.original_amount)
            else Credit.STATUS_PAID
            if expected_remaining == ZERO_MONEY
            else Credit.STATUS_PARTIAL
        )

        if _money(credit.remaining_amount) != expected_remaining:
            _append(
                section,
                "critical",
                f"Credit #{credit.id} remaining mismatch: stored={credit.remaining_amount}, expected={expected_remaining}.",
            )
        if credit.status != expected_status:
            _append(
                section,
                "warning",
                f"Credit #{credit.id} status mismatch: stored={credit.status}, expected={expected_status}.",
            )

    if not any(section[level] for level in ("critical", "warning")):
        _append(section, "info", "No client debt integrity problems found.")
    return section


def _audit_suppliers():
    section = _new_section("Suppliers")

    purchase_totals = _supplier_purchase_totals()
    active_payments = SupplierPayment.objects.select_related("store", "supplier", "purchase").filter(
        status=SupplierPayment.STATUS_ACTIVE
    )
    for payment in active_payments:
        allocation_total = _money(
            payment.allocations.aggregate(total=Coalesce(Sum("amount"), Value(ZERO_MONEY)))["total"]
        )
        try:
            overpayment_total = _money(payment.overpayment.remaining_amount)
        except SupplierOverpayment.DoesNotExist:
            overpayment_total = ZERO_MONEY
        covered_total = _money(allocation_total + overpayment_total)
        if not payment.allocations.exists() and overpayment_total == ZERO_MONEY:
            _append(
                section,
                "critical",
                f"Active supplier payment #{payment.id} ({payment.supplier.name}, {payment.amount}) has no allocations or overpayment record.",
            )
        if covered_total > _money(payment.amount):
            _append(
                section,
                "critical",
                f"Supplier payment #{payment.id} coverage {covered_total} exceeds payment amount {payment.amount}.",
            )
        if covered_total != _money(payment.amount):
            _append(
                section,
                "warning",
                f"Supplier payment #{payment.id} coverage {covered_total} differs from payment amount {payment.amount}.",
            )

    cancelled_payments = SupplierPayment.objects.select_related("store", "supplier").filter(
        status=SupplierPayment.STATUS_CANCELLED
    )
    for payment in cancelled_payments:
        if payment.allocations.exists() or hasattr(payment, "overpayment"):
            _append(
                section,
                "warning",
                f"Cancelled supplier payment #{payment.id} still has allocation rows or overpayment state.",
            )

    purchase_paid = defaultdict(lambda: ZERO_MONEY)
    for allocation in SupplierPaymentAllocation.objects.select_related("payment", "purchase").filter(
        payment__status=SupplierPayment.STATUS_ACTIVE,
        purchase__deleted_at__isnull=True,
    ):
        purchase_paid[(allocation.purchase_id, allocation.store_id)] += allocation.amount

    for key, meta in purchase_totals.items():
        allocated = _money(purchase_paid.get(key, ZERO_MONEY))
        if allocated > meta["total"]:
            _append(
                section,
                "critical",
                f"Purchase #{key[0]} store #{key[1]} overpaid: allocated={allocated}, total={meta['total']}.",
            )

    for overpayment in SupplierOverpayment.objects.select_related("supplier", "store", "source_payment"):
        if overpayment.remaining_amount < ZERO_MONEY:
            _append(
                section,
                "critical",
                f"Supplier overpayment #{overpayment.id} is negative: {overpayment.remaining_amount}.",
            )
        if overpayment.amount < overpayment.remaining_amount:
            _append(
                section,
                "critical",
                f"Supplier overpayment #{overpayment.id} remaining amount exceeds original amount.",
            )

    if not any(section[level] for level in ("critical", "warning")):
        _append(section, "info", "No supplier debt integrity problems found.")
    return section


def _audit_cash():
    section = _new_section("Cash")

    for store in Store.objects.order_by("name"):
        breakdown = build_cash_breakdown(store)
        if breakdown["difference"] != ZERO_MONEY:
            _append(
                section,
                "critical",
                (
                    f"Cash mismatch for store {store.name}: stored={breakdown['stored_balance']}, "
                    f"expected={breakdown['formula_balance']}, diff={breakdown['difference']}. "
                    f"cash_sales={breakdown['cash_sales']}, client_cash={breakdown['client_debt_payments']}, "
                    f"legacy_credit={breakdown['legacy_credit_payments']}, supplier_cash={breakdown['supplier_payments']}, "
                    f"advances={breakdown['employee_advances']}, store_expenses={breakdown['store_expenses']}, "
                    f"salary={breakdown['salary_payments']}."
                ),
            )
        else:
            _append(
                section,
                "info",
                f"Cash OK for store {store.name}: {breakdown['stored_balance']}.",
            )

    return section


def _audit_reports():
    section = _new_section("Reports")

    debtor_rows = build_debtor_rows()
    debtor_total = _money(sum((row["total_debt"] for row in debtor_rows), ZERO_MONEY))
    model_debtor_total = _money(
        Credit.objects.filter(sale__deleted_at__isnull=True).exclude(status=Credit.STATUS_PAID).aggregate(
            total=Coalesce(Sum("remaining_amount"), Value(ZERO_MONEY))
        )["total"]
    )
    if debtor_total != model_debtor_total:
        _append(
            section,
            "critical",
            f"Debtors report total mismatch: report={debtor_total}, model={model_debtor_total}.",
        )

    profitability_rows = build_product_profitability_rows(group_by_store=True)
    profitability_revenue = _money(sum((row["revenue"] for row in profitability_rows), ZERO_MONEY))
    profitability_cost = _money(sum((row["sold_cost"] for row in profitability_rows), ZERO_MONEY))
    model_revenue = _money(
        Sale.objects.filter(deleted_at__isnull=True).aggregate(
            total=Coalesce(Sum("total_amount"), Value(ZERO_MONEY))
        )["total"]
    )
    batch_cost = _money(
        SaleItemBatch.objects.filter(
            sale_item__sale__deleted_at__isnull=True,
            purchase_item__purchase__deleted_at__isnull=True,
        )
        .aggregate(
            total=Coalesce(
                Sum(
                    ExpressionWrapper(
                        F("quantity") * F("purchase_item__purchase_price_per_kg"),
                        output_field=MONEY_FIELD,
                    )
                ),
                Value(ZERO_MONEY),
            )
        )["total"]
    )
    if profitability_revenue != model_revenue:
        _append(
            section,
            "critical",
            f"Product profitability revenue mismatch: report={profitability_revenue}, model={model_revenue}.",
        )
    if profitability_cost != batch_cost:
        _append(
            section,
            "critical",
            f"Product profitability cost mismatch: report={profitability_cost}, batches={batch_cost}.",
        )

    today = timezone.localdate()
    for store in Store.objects.order_by("name"):
        sale_total = _money(
            Sale.objects.filter(store=store, date=today, deleted_at__isnull=True).aggregate(
                total=Coalesce(Sum("total_amount"), Value(ZERO_MONEY))
            )["total"]
        )
        store_expense_total = _money(
            StoreExpense.objects.filter(store=store, date=today, deleted_at__isnull=True).aggregate(
                total=Coalesce(Sum("amount"), Value(ZERO_MONEY))
            )["total"]
        )
        employee_expense_total = _money(
            Expense.objects.filter(store=store, date=today).aggregate(
                total=Coalesce(Sum("amount"), Value(ZERO_MONEY))
            )["total"]
        )
        salary_total = _money(
            SalaryPayment.objects.filter(store=store, date=today).aggregate(
                total=Coalesce(Sum("amount"), Value(ZERO_MONEY))
            )["total"]
        )
        _append(
            section,
            "info",
            f"Daily report baseline for {store.name}: sales={sale_total}, employee_expenses={employee_expense_total}, store_expenses={store_expense_total}, salary={salary_total}.",
        )

    if not any(section[level] for level in ("critical", "warning")):
        _append(section, "info", "No report consistency problems found.")
    return section
