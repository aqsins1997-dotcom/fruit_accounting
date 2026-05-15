from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, Sum, Value
from django.db.models import Prefetch
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.models import Store

from .models import CashRegister, Sale, SaleItem, SaleItemBatch

ZERO = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _sum_amount(queryset, field_name="amount"):
    return queryset.aggregate(
        total=Coalesce(Sum(field_name), Value(ZERO, output_field=MONEY_FIELD))
    )["total"] or ZERO


def build_cash_breakdown(store):
    from apps.credits.models import ClientDebtPayment, CreditPayment
    from apps.expenses.models import EmployeeAdvance, SalaryPayment, StoreExpense
    from apps.payables.models import SupplierPayment

    cash_sales = _sum_amount(
        Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CASH,
            deleted_at__isnull=True,
        ),
        "total_amount",
    )

    credit_sales = _sum_amount(
        Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            deleted_at__isnull=True,
        ),
        "total_amount",
    )

    client_debt_payment_queryset = ClientDebtPayment.objects.filter(store=store)
    client_debt_payments = _sum_amount(client_debt_payment_queryset)
    client_debt_payments_by_method = {
        row["payment_method"]: {
            "count": row["count"],
            "total": _money(row["total"]),
        }
        for row in client_debt_payment_queryset.values("payment_method").annotate(
            count=Count("id"),
            total=Coalesce(Sum("amount"), Value(ZERO, output_field=MONEY_FIELD)),
        )
    }

    legacy_credit_payments = _sum_amount(
        CreditPayment.objects.filter(
            credit__store=store,
            credit__sale__deleted_at__isnull=True,
            client_debt_payment__isnull=True,
        )
    )
    supplier_payments = _sum_amount(SupplierPayment.objects.filter(store=store))
    employee_advances = _sum_amount(EmployeeAdvance.objects.filter(store=store))
    store_expenses = _sum_amount(StoreExpense.objects.filter(store=store))
    salary_payments = _sum_amount(SalaryPayment.objects.filter(store=store))

    formula_balance = _money(
        cash_sales
        + client_debt_payments
        + legacy_credit_payments
        - supplier_payments
        - employee_advances
        - store_expenses
        - salary_payments
    )
    register = CashRegister.objects.filter(store=store).first()
    stored_balance = _money(register.balance if register else ZERO)

    return {
        "store": store,
        "stored_balance": stored_balance,
        "formula_balance": formula_balance,
        "difference": _money(stored_balance - formula_balance),
        "cash_sales": _money(cash_sales),
        "cash_sales_count": Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CASH,
            deleted_at__isnull=True,
        ).count(),
        "credit_sales": _money(credit_sales),
        "credit_sales_count": Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            deleted_at__isnull=True,
        ).count(),
        "client_debt_payments": _money(client_debt_payments),
        "client_debt_payments_count": client_debt_payment_queryset.count(),
        "client_debt_payments_by_method": client_debt_payments_by_method,
        "legacy_credit_payments": _money(legacy_credit_payments),
        "supplier_payments": _money(supplier_payments),
        "employee_advances": _money(employee_advances),
        "store_expenses": _money(store_expenses),
        "salary_payments": _money(salary_payments),
    }


def calculate_cash_balance(store):
    return build_cash_breakdown(store)["formula_balance"]


@transaction.atomic
def recalculate_sale_costs_for_purchase_item(purchase_item):
    sale_item_ids = list(
        SaleItemBatch.objects.filter(
            purchase_item=purchase_item,
            sale_item__sale__deleted_at__isnull=True,
        )
        .values_list("sale_item_id", flat=True)
        .distinct()
    )
    if not sale_item_ids:
        return []

    now = timezone.now()
    changed_sale_ids = set()
    changed_sale_item_ids = []

    sale_items = (
        SaleItem.objects.select_for_update()
        .filter(id__in=sale_item_ids, sale__deleted_at__isnull=True)
        .prefetch_related(
            Prefetch(
                "batches",
                queryset=SaleItemBatch.objects.select_related("purchase_item").filter(
                    purchase_item__purchase__deleted_at__isnull=True,
                    sale_item__sale__deleted_at__isnull=True,
                ),
            )
        )
    )
    for sale_item in sale_items:
        total_cost = sum(
            (
                batch.quantity * batch.purchase_item.purchase_price_per_kg
                for batch in sale_item.batches.all()
            ),
            ZERO,
        )
        line_cost_total = _money(total_cost)
        if sale_item.quantity_kg > Decimal("0.000"):
            cost_price_per_kg = _money(total_cost / sale_item.quantity_kg)
        else:
            cost_price_per_kg = ZERO
        profit = _money(sale_item.line_total - line_cost_total)

        SaleItem.objects.filter(pk=sale_item.pk).update(
            cost_price_per_kg=cost_price_per_kg,
            line_cost_total=line_cost_total,
            profit=profit,
            updated_at=now,
        )
        changed_sale_ids.add(sale_item.sale_id)
        changed_sale_item_ids.append(sale_item.id)

    for sale in Sale.objects.select_for_update().filter(
        id__in=changed_sale_ids,
        deleted_at__isnull=True,
    ):
        totals = SaleItem.objects.filter(sale=sale).aggregate(
            total_cost=Coalesce(Sum("line_cost_total"), Value(ZERO, output_field=MONEY_FIELD)),
            total_profit=Coalesce(Sum("profit"), Value(ZERO, output_field=MONEY_FIELD)),
        )
        Sale.objects.filter(pk=sale.pk).update(
            total_cost=_money(totals["total_cost"]),
            total_profit=_money(totals["total_profit"]),
            updated_at=now,
        )

    return changed_sale_item_ids


@transaction.atomic
def recalculate_cash_registers(*, store=None):
    stores = Store.objects.filter(pk=store.pk) if store else Store.objects.all()
    results = []

    for target_store in stores.order_by("name"):
        register, _ = CashRegister.objects.select_for_update().get_or_create(
            store=target_store,
            defaults={"balance": ZERO},
        )
        old_balance = register.balance
        new_balance = calculate_cash_balance(target_store)
        register.balance = new_balance
        register.save(update_fields=["balance", "updated_at"])
        results.append(
            {
                "store": target_store,
                "old_balance": old_balance,
                "new_balance": new_balance,
            }
        )

    return results
