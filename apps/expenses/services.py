from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import CashRegister, Sale

from .models import EmployeeAdvance, Expense, SalaryPayment, StoreExpense

ZERO = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def _money_value():
    return Value(ZERO, output_field=MONEY_FIELD)


def _sum_amount(queryset, field_name="amount"):
    return queryset.aggregate(total=Coalesce(Sum(field_name), _money_value()))["total"] or ZERO


def _save_cash_register(register):
    register.save(update_fields=["balance", "updated_at"])


def _get_cash_register(store):
    register, _ = CashRegister.objects.get_or_create(
        store=store,
        defaults={"balance": ZERO},
    )
    return register


def _validate_cash_outflow(*, store, amount, available_amount):
    if amount > available_amount:
        raise ValidationError(
            {
                "amount": (
                    "Недостаточно денег в кассе магазина. "
                    f"Доступно: {available_amount}."
                )
            }
        )


def _apply_cash_outflow(instance, *, previous_instance=None):
    if previous_instance and previous_instance.store_id == instance.store_id:
        register = _get_cash_register(instance.store)
        available_amount = register.balance + previous_instance.amount
        _validate_cash_outflow(
            store=instance.store,
            amount=instance.amount,
            available_amount=available_amount,
        )
        register.balance = available_amount - instance.amount
        _save_cash_register(register)
        return

    if previous_instance:
        previous_register = _get_cash_register(previous_instance.store)
        previous_register.balance += previous_instance.amount
        _save_cash_register(previous_register)

    register = _get_cash_register(instance.store)
    _validate_cash_outflow(
        store=instance.store,
        amount=instance.amount,
        available_amount=register.balance,
    )
    register.balance -= instance.amount
    _save_cash_register(register)


def get_advances_queryset(*, store=None, seller=None, date_from=None, date_to=None):
    queryset = EmployeeAdvance.objects.select_related("store", "seller").all()
    if store:
        queryset = queryset.filter(store=store)
    if seller:
        queryset = queryset.filter(seller=seller)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)
    return queryset


def get_expenses_queryset(*, store=None, seller=None, category=None, date_from=None, date_to=None):
    queryset = Expense.objects.select_related("store", "seller", "category", "advance").all()
    if store:
        queryset = queryset.filter(store=store)
    if seller:
        queryset = queryset.filter(seller=seller)
    if category:
        queryset = queryset.filter(category=category)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)
    return queryset


def get_store_expenses_queryset(*, store=None, category=None, date_from=None, date_to=None):
    queryset = StoreExpense.objects.select_related("store", "category").all()
    if store:
        queryset = queryset.filter(store=store)
    if category:
        queryset = queryset.filter(category=category)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)
    return queryset


def get_salary_payments_queryset(*, store=None, seller=None, date_from=None, date_to=None):
    queryset = SalaryPayment.objects.select_related("store", "seller").all()
    if store:
        queryset = queryset.filter(store=store)
    if seller:
        queryset = queryset.filter(seller=seller)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)
    return queryset


def get_employee_remaining_balance(*, store, seller, date_to=None, exclude_expense_id=None):
    advances = get_advances_queryset(store=store, seller=seller, date_to=date_to)
    expenses = get_expenses_queryset(store=store, seller=seller, date_to=date_to)
    if exclude_expense_id:
        expenses = expenses.exclude(pk=exclude_expense_id)
    return _sum_amount(advances) - _sum_amount(expenses)


def get_advance_remaining_balance(*, advance, exclude_expense_id=None):
    expenses = advance.expenses.all()
    if exclude_expense_id:
        expenses = expenses.exclude(pk=exclude_expense_id)
    return advance.amount - _sum_amount(expenses)


def validate_expense_balance(*, expense, allow_overrun=False):
    if allow_overrun:
        return

    total_remaining = get_employee_remaining_balance(
        store=expense.store,
        seller=expense.seller,
        date_to=expense.date,
        exclude_expense_id=expense.pk,
    )
    if expense.amount > total_remaining:
        raise ValidationError(
            {
                "amount": (
                    "Расход превышает доступный подотчетный остаток сотрудника. "
                    f"Доступно: {total_remaining}."
                )
            }
        )

    if expense.advance_id:
        advance_remaining = get_advance_remaining_balance(
            advance=expense.advance,
            exclude_expense_id=expense.pk,
        )
        if expense.amount > advance_remaining:
            raise ValidationError(
                {
                    "advance": (
                        "Расход превышает остаток выбранного аванса. "
                        f"Доступно по авансу: {advance_remaining}."
                    )
                }
            )


@transaction.atomic
def save_employee_advance(advance):
    previous_instance = None
    if advance.pk:
        previous_instance = EmployeeAdvance.objects.get(pk=advance.pk)

    advance.full_clean()
    _apply_cash_outflow(advance, previous_instance=previous_instance)
    advance.save()
    return advance


@transaction.atomic
def save_expense(expense, *, allow_overrun=False):
    expense._allow_overrun = allow_overrun
    expense.full_clean()
    expense.save()
    return expense


@transaction.atomic
def save_store_expense(store_expense):
    previous_instance = None
    if store_expense.pk:
        previous_instance = StoreExpense.objects.get(pk=store_expense.pk)

    store_expense.full_clean()
    _apply_cash_outflow(store_expense, previous_instance=previous_instance)
    store_expense.save()
    return store_expense


@transaction.atomic
def save_salary_payment(salary_payment):
    previous_instance = None
    if salary_payment.pk:
        previous_instance = SalaryPayment.objects.get(pk=salary_payment.pk)

    salary_payment.full_clean()
    _apply_cash_outflow(salary_payment, previous_instance=previous_instance)
    salary_payment.save()
    return salary_payment


def build_employee_balance_report(*, store=None, seller=None, date_to=None, today=None):
    today = today or timezone.localdate()
    advances = get_advances_queryset(store=store, seller=seller, date_to=date_to)
    expenses = get_expenses_queryset(store=store, seller=seller, date_to=date_to)
    salary_payments = get_salary_payments_queryset(store=store, seller=seller, date_to=date_to)

    advance_rows = advances.values(
        "store_id",
        "store__name",
        "seller_id",
        "seller__name",
    ).annotate(issued_amount=Coalesce(Sum("amount"), _money_value()))

    expense_rows = expenses.values(
        "store_id",
        "store__name",
        "seller_id",
        "seller__name",
    ).annotate(expense_amount=Coalesce(Sum("amount"), _money_value()))

    salary_rows = salary_payments.values(
        "store_id",
        "store__name",
        "seller_id",
        "seller__name",
    ).annotate(salary_amount=Coalesce(Sum("amount"), _money_value()))

    advance_today_rows = advances.filter(date=today).values(
        "store_id",
        "seller_id",
    ).annotate(today_advance_amount=Coalesce(Sum("amount"), _money_value()))

    salary_today_rows = salary_payments.filter(date=today).values(
        "store_id",
        "seller_id",
    ).annotate(today_salary_amount=Coalesce(Sum("amount"), _money_value()))

    rows_map = {}
    for row in advance_rows:
        key = (row["store_id"], row["seller_id"])
        rows_map[key] = {
            "store_id": row["store_id"],
            "store_name": row["store__name"],
            "seller_id": row["seller_id"],
            "seller_name": row["seller__name"],
            "issued_amount": row["issued_amount"] or ZERO,
            "expense_amount": ZERO,
            "salary_amount": ZERO,
            "today_advance_amount": ZERO,
            "today_salary_amount": ZERO,
        }

    for row in expense_rows:
        key = (row["store_id"], row["seller_id"])
        target = rows_map.setdefault(
            key,
            {
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "seller_id": row["seller_id"],
                "seller_name": row["seller__name"],
                "issued_amount": ZERO,
                "expense_amount": ZERO,
                "salary_amount": ZERO,
                "today_advance_amount": ZERO,
                "today_salary_amount": ZERO,
            },
        )
        target["expense_amount"] = row["expense_amount"] or ZERO

    for row in salary_rows:
        key = (row["store_id"], row["seller_id"])
        target = rows_map.setdefault(
            key,
            {
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "seller_id": row["seller_id"],
                "seller_name": row["seller__name"],
                "issued_amount": ZERO,
                "expense_amount": ZERO,
                "salary_amount": ZERO,
                "today_advance_amount": ZERO,
                "today_salary_amount": ZERO,
            },
        )
        target["salary_amount"] = row["salary_amount"] or ZERO

    for row in advance_today_rows:
        key = (row["store_id"], row["seller_id"])
        target = rows_map.setdefault(
            key,
            {
                "store_id": row["store_id"],
                "store_name": "",
                "seller_id": row["seller_id"],
                "seller_name": "",
                "issued_amount": ZERO,
                "expense_amount": ZERO,
                "salary_amount": ZERO,
                "today_advance_amount": ZERO,
                "today_salary_amount": ZERO,
            },
        )
        target["today_advance_amount"] = row["today_advance_amount"] or ZERO

    for row in salary_today_rows:
        key = (row["store_id"], row["seller_id"])
        target = rows_map.setdefault(
            key,
            {
                "store_id": row["store_id"],
                "store_name": "",
                "seller_id": row["seller_id"],
                "seller_name": "",
                "issued_amount": ZERO,
                "expense_amount": ZERO,
                "salary_amount": ZERO,
                "today_advance_amount": ZERO,
                "today_salary_amount": ZERO,
            },
        )
        target["today_salary_amount"] = row["today_salary_amount"] or ZERO

    rows = []
    for row in rows_map.values():
        row["remaining_amount"] = row["issued_amount"] - row["expense_amount"]
        row["today_taken_amount"] = row["today_advance_amount"] + row["today_salary_amount"]
        rows.append(row)

    rows.sort(key=lambda item: (item["store_name"], item["seller_name"]))

    summary = {
        "issued_amount": sum((row["issued_amount"] for row in rows), ZERO),
        "expense_amount": sum((row["expense_amount"] for row in rows), ZERO),
        "remaining_amount": sum((row["remaining_amount"] for row in rows), ZERO),
        "salary_amount": sum((row["salary_amount"] for row in rows), ZERO),
        "today_advance_amount": sum((row["today_advance_amount"] for row in rows), ZERO),
        "today_salary_amount": sum((row["today_salary_amount"] for row in rows), ZERO),
        "today_taken_amount": sum((row["today_taken_amount"] for row in rows), ZERO),
    }
    return rows, summary


def _merge_amount_row(container, key, base_row, field_name, value):
    row = container.setdefault(key, base_row)
    row[field_name] = value or ZERO
    total_fields = [name for name in row.keys() if name.endswith("_amount")]
    row["total_amount"] = sum((row[name] for name in total_fields if name != "total_amount"), ZERO)
    return row


def build_expense_report(*, store=None, seller=None, category=None, date_from=None, date_to=None):
    employee_expense_queryset = get_expenses_queryset(
        store=store,
        seller=seller,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )
    store_expense_queryset = get_store_expenses_queryset(
        store=store,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )
    if category:
        salary_queryset = SalaryPayment.objects.none().select_related("store", "seller")
    else:
        salary_queryset = get_salary_payments_queryset(
            store=store,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
        )
    advance_queryset = get_advances_queryset(
        store=store,
        seller=seller,
        date_from=date_from,
        date_to=date_to,
    )

    sales_queryset = Sale.objects.all()
    if store:
        sales_queryset = sales_queryset.filter(store=store)
    if date_from:
        sales_queryset = sales_queryset.filter(date__gte=date_from)
    if date_to:
        sales_queryset = sales_queryset.filter(date__lte=date_to)

    total_employee_expenses = _sum_amount(employee_expense_queryset)
    total_store_expenses = _sum_amount(store_expense_queryset)
    total_salary_payments = _sum_amount(salary_queryset)
    total_expenses = total_employee_expenses + total_store_expenses + total_salary_payments
    advances_issued = _sum_amount(advance_queryset)
    total_revenue = _sum_amount(sales_queryset, "total_amount")
    total_cost = _sum_amount(sales_queryset, "total_cost")
    gross_profit = total_revenue - total_cost
    other_expenses = ZERO
    net_profit = gross_profit - total_expenses - other_expenses

    balances_rows, balance_summary = build_employee_balance_report(
        store=store,
        seller=seller,
        date_to=date_to,
    )

    store_map = {}
    for row in employee_expense_queryset.values("store_id", "store__name").annotate(
        employee_expense_amount=Coalesce(Sum("amount"), _money_value())
    ):
        _merge_amount_row(
            store_map,
            row["store_id"],
            {
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "employee_expense_amount": ZERO,
                "store_expense_amount": ZERO,
                "salary_amount": ZERO,
                "total_amount": ZERO,
            },
            "employee_expense_amount",
            row["employee_expense_amount"],
        )
    for row in store_expense_queryset.values("store_id", "store__name").annotate(
        store_expense_amount=Coalesce(Sum("amount"), _money_value())
    ):
        _merge_amount_row(
            store_map,
            row["store_id"],
            {
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "employee_expense_amount": ZERO,
                "store_expense_amount": ZERO,
                "salary_amount": ZERO,
                "total_amount": ZERO,
            },
            "store_expense_amount",
            row["store_expense_amount"],
        )
    for row in salary_queryset.values("store_id", "store__name").annotate(
        salary_amount=Coalesce(Sum("amount"), _money_value())
    ):
        _merge_amount_row(
            store_map,
            row["store_id"],
            {
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "employee_expense_amount": ZERO,
                "store_expense_amount": ZERO,
                "salary_amount": ZERO,
                "total_amount": ZERO,
            },
            "salary_amount",
            row["salary_amount"],
        )

    seller_map = {}
    for row in employee_expense_queryset.values("seller_id", "seller__name", "store__name").annotate(
        employee_expense_amount=Coalesce(Sum("amount"), _money_value())
    ):
        _merge_amount_row(
            seller_map,
            row["seller_id"],
            {
                "seller_id": row["seller_id"],
                "seller_name": row["seller__name"],
                "store_name": row["store__name"],
                "employee_expense_amount": ZERO,
                "salary_amount": ZERO,
                "total_amount": ZERO,
            },
            "employee_expense_amount",
            row["employee_expense_amount"],
        )
    for row in salary_queryset.values("seller_id", "seller__name", "store__name").annotate(
        salary_amount=Coalesce(Sum("amount"), _money_value())
    ):
        _merge_amount_row(
            seller_map,
            row["seller_id"],
            {
                "seller_id": row["seller_id"],
                "seller_name": row["seller__name"],
                "store_name": row["store__name"],
                "employee_expense_amount": ZERO,
                "salary_amount": ZERO,
                "total_amount": ZERO,
            },
            "salary_amount",
            row["salary_amount"],
        )

    category_map = {}
    for row in employee_expense_queryset.values("category_id", "category__name").annotate(
        total_amount=Coalesce(Sum("amount"), _money_value())
    ):
        category_map[row["category_id"]] = {
            "category_name": row["category__name"],
            "total_amount": row["total_amount"] or ZERO,
        }
    for row in store_expense_queryset.values("category_id", "category__name").annotate(
        total_amount=Coalesce(Sum("amount"), _money_value())
    ):
        target = category_map.setdefault(
            row["category_id"],
            {
                "category_name": row["category__name"],
                "total_amount": ZERO,
            },
        )
        target["total_amount"] += row["total_amount"] or ZERO
    if total_salary_payments:
        category_map["salary"] = {
            "category_name": "Зарплата",
            "total_amount": total_salary_payments,
        }

    detailed_rows = []
    for expense in employee_expense_queryset:
        detailed_rows.append(
            {
                "date": expense.date,
                "type_label": "Расход сотрудника",
                "store_name": expense.store.name,
                "seller_name": expense.seller.name,
                "category_name": expense.category.name,
                "source_name": f"Аванс №{expense.advance_id}" if expense.advance_id else "Без привязки",
                "amount": expense.amount,
                "comment": expense.comment,
            }
        )
    for store_expense in store_expense_queryset:
        detailed_rows.append(
            {
                "date": store_expense.date,
                "type_label": "Расход магазина",
                "store_name": store_expense.store.name,
                "seller_name": "—",
                "category_name": store_expense.category.name,
                "source_name": "Касса магазина",
                "amount": store_expense.amount,
                "comment": store_expense.comment,
            }
        )
    for salary_payment in salary_queryset:
        detailed_rows.append(
            {
                "date": salary_payment.date,
                "type_label": "Зарплата",
                "store_name": salary_payment.store.name,
                "seller_name": salary_payment.seller.name,
                "category_name": "Зарплата",
                "source_name": "Касса магазина",
                "amount": salary_payment.amount,
                "comment": salary_payment.comment,
            }
        )
    detailed_rows.sort(key=lambda row: (row["date"], row["amount"]), reverse=True)

    return {
        "expenses": detailed_rows,
        "by_store": sorted(store_map.values(), key=lambda row: row["store_name"]),
        "by_seller": sorted(seller_map.values(), key=lambda row: (row["store_name"], row["seller_name"])),
        "by_category": sorted(category_map.values(), key=lambda row: row["category_name"]),
        "summary": {
            "advances_issued": advances_issued,
            "employee_expenses": total_employee_expenses,
            "store_expenses": total_store_expenses,
            "salary_payments": total_salary_payments,
            "total_expenses": total_expenses,
            "total_expense_count": (
                employee_expense_queryset.count()
                + store_expense_queryset.count()
                + salary_queryset.count()
            ),
            "total_revenue": total_revenue,
            "total_cost": total_cost,
            "gross_profit": gross_profit,
            "other_expenses": other_expenses,
            "net_profit": net_profit,
            "outstanding_advances": balance_summary["remaining_amount"],
        },
        "balances": balances_rows,
    }
