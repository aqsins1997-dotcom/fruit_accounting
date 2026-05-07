from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from apps.core.models import Store

from .models import CashRegister, Sale

ZERO = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _sum_amount(queryset, field_name="amount"):
    return queryset.aggregate(
        total=Coalesce(Sum(field_name), Value(ZERO, output_field=MONEY_FIELD))
    )["total"] or ZERO


def calculate_cash_balance(store):
    from apps.credits.models import CreditPayment
    from apps.expenses.models import EmployeeAdvance, SalaryPayment, StoreExpense
    from apps.payables.models import SupplierPayment

    cash_sales = _sum_amount(
        Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CASH,
        ),
        "total_amount",
    )
    credit_payments = _sum_amount(CreditPayment.objects.filter(credit__store=store))
    supplier_payments = _sum_amount(SupplierPayment.objects.filter(store=store))
    employee_advances = _sum_amount(EmployeeAdvance.objects.filter(store=store))
    store_expenses = _sum_amount(StoreExpense.objects.filter(store=store))
    salary_payments = _sum_amount(SalaryPayment.objects.filter(store=store))

    return _money(
        cash_sales
        + credit_payments
        - supplier_payments
        - employee_advances
        - store_expenses
        - salary_payments
    )


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
