from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

ZERO = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def _money_value():
    return Value(ZERO, output_field=MONEY_FIELD)


def _sum_amount(queryset, field_name="amount"):
    return queryset.aggregate(total=Coalesce(Sum(field_name), _money_value()))["total"] or ZERO


def get_client_debt(*, store_id=None, client_id=None, store=None, client=None, exclude_payment_id=None):
    from .models import ClientDebtPayment, Credit, CreditPayment

    if store is not None:
        store_id = store.pk
    if client is not None:
        client_id = client.pk

    if not store_id or not client_id:
        return ZERO

    total_credit = _sum_amount(
        Credit.objects.filter(
            store_id=store_id,
            customer_id=client_id,
            sale__deleted_at__isnull=True,
        ),
        "original_amount",
    )

    client_payments = ClientDebtPayment.objects.filter(store_id=store_id, client_id=client_id)
    if exclude_payment_id:
        client_payments = client_payments.exclude(pk=exclude_payment_id)

    total_paid = _sum_amount(client_payments)
    total_paid += _sum_amount(
        CreditPayment.objects.filter(
            credit__store_id=store_id,
            credit__customer_id=client_id,
            credit__sale__deleted_at__isnull=True,
            client_debt_payment__isnull=True,
        )
    )

    debt = total_credit - total_paid
    if debt < ZERO:
        return ZERO
    return debt


def build_debtor_rows(*, store=None, search=""):
    from .models import ClientDebtPayment, Credit, CreditPayment

    credits = Credit.objects.select_related("store", "customer").filter(
        sale__deleted_at__isnull=True
    )
    client_payments = ClientDebtPayment.objects.select_related("store", "client").all()
    legacy_payments = CreditPayment.objects.filter(client_debt_payment__isnull=True).select_related(
        "credit__store",
        "credit__customer",
    ).filter(credit__sale__deleted_at__isnull=True)

    if store:
        credits = credits.filter(store=store)
        client_payments = client_payments.filter(store=store)
        legacy_payments = legacy_payments.filter(credit__store=store)

    if search:
        credits = credits.filter(
            Q(customer__name__icontains=search) | Q(customer__phone__icontains=search)
        )
        client_payments = client_payments.filter(
            Q(client__name__icontains=search) | Q(client__phone__icontains=search)
        )
        legacy_payments = legacy_payments.filter(
            Q(credit__customer__name__icontains=search) |
            Q(credit__customer__phone__icontains=search)
        )

    credit_rows = credits.values(
        "store_id",
        "store__name",
        "customer_id",
        "customer__name",
        "customer__phone",
    ).annotate(total_taken=Coalesce(Sum("original_amount"), _money_value()))

    payment_totals = {
        (row["store_id"], row["client_id"]): row["total_paid"] or ZERO
        for row in client_payments.values("store_id", "client_id").annotate(
            total_paid=Coalesce(Sum("amount"), _money_value())
        )
    }
    legacy_payment_totals = {
        (row["credit__store_id"], row["credit__customer_id"]): row["total_paid"] or ZERO
        for row in legacy_payments.values("credit__store_id", "credit__customer_id").annotate(
            total_paid=Coalesce(Sum("amount"), _money_value())
        )
    }

    rows = []
    for row in credit_rows:
        key = (row["store_id"], row["customer_id"])
        total_taken = row["total_taken"] or ZERO
        total_paid = payment_totals.get(key, ZERO) + legacy_payment_totals.get(key, ZERO)
        total_debt = total_taken - total_paid

        if total_debt <= ZERO:
            continue

        rows.append(
            {
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "customer_id": row["customer_id"],
                "customer_name": row["customer__name"],
                "customer_phone": row["customer__phone"],
                "total_taken": total_taken,
                "total_paid": total_paid,
                "total_debt": total_debt,
            }
        )

    rows.sort(key=lambda item: (item["store_name"], -item["total_debt"], item["customer_name"]))
    return rows


def summarize_debt_by_store(rows):
    store_rows = {}

    for row in rows:
        store_id = row["store_id"]
        target = store_rows.setdefault(
            store_id,
            {
                "store_id": store_id,
                "store__name": row["store_name"],
                "total_debt": ZERO,
            },
        )
        target["total_debt"] += row["total_debt"]

    return sorted(store_rows.values(), key=lambda item: item["store__name"])
