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

    active_allocations = CreditPayment.objects.filter(
        credit__store_id=store_id,
        credit__customer_id=client_id,
        credit__sale__deleted_at__isnull=True,
        client_debt_payment__status=ClientDebtPayment.STATUS_ACTIVE,
    )
    if exclude_payment_id:
        active_allocations = active_allocations.exclude(client_debt_payment_id=exclude_payment_id)

    total_paid = _sum_amount(active_allocations)
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
    active_allocations = CreditPayment.objects.filter(
        client_debt_payment__status=ClientDebtPayment.STATUS_ACTIVE,
        credit__sale__deleted_at__isnull=True,
    )
    legacy_payments = CreditPayment.objects.filter(client_debt_payment__isnull=True).select_related(
        "credit__store",
        "credit__customer",
    ).filter(credit__sale__deleted_at__isnull=True)

    if store:
        credits = credits.filter(store=store)
        active_allocations = active_allocations.filter(credit__store=store)
        legacy_payments = legacy_payments.filter(credit__store=store)

    if search:
        credits = credits.filter(
            Q(customer__name__icontains=search) | Q(customer__phone__icontains=search)
        )
        active_allocations = active_allocations.filter(
            Q(credit__customer__name__icontains=search) |
            Q(credit__customer__phone__icontains=search)
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
        (row["credit__store_id"], row["credit__customer_id"]): row["total_paid"] or ZERO
        for row in active_allocations.values("credit__store_id", "credit__customer_id").annotate(
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


def repair_client_payment_allocations():
    from .models import ClientDebtPayment, Credit, CreditPayment

    payments = list(
        ClientDebtPayment.objects.select_related("store", "client")
        .filter(status=ClientDebtPayment.STATUS_ACTIVE, allocations__isnull=True)
        .order_by("paid_at", "id")
        .distinct()
    )

    report = {
        "found_count": len(payments),
        "clients": [],
        "payments": [],
        "unallocated": [],
        "total_allocated": ZERO,
    }

    if not payments:
        return report

    seen_clients = set()
    for payment in payments:
        client_key = (payment.store_id, payment.client_id)
        if client_key not in seen_clients:
            seen_clients.add(client_key)
            report["clients"].append(
                {
                    "store_id": payment.store_id,
                    "store_name": payment.store.name,
                    "client_id": payment.client_id,
                    "client_name": payment.client.name,
                }
            )

        remaining_to_allocate = payment.amount or ZERO
        created_allocations = []
        credits = (
            Credit.objects.select_for_update()
            .filter(
                store_id=payment.store_id,
                customer_id=payment.client_id,
                sale__deleted_at__isnull=True,
            )
            .exclude(status=Credit.STATUS_PAID)
            .order_by("sale__date", "id")
        )

        for credit in credits:
            if remaining_to_allocate <= ZERO:
                break

            available_amount = credit.remaining_amount or ZERO
            if available_amount <= ZERO:
                continue

            allocated_amount = min(available_amount, remaining_to_allocate)
            allocation = CreditPayment.objects.create(
                credit=credit,
                client_debt_payment=payment,
                date=payment.paid_at,
                amount=allocated_amount,
                comment=payment.comment,
            )
            created_allocations.append(allocation)
            remaining_to_allocate -= allocated_amount

        allocated_total = (payment.amount or ZERO) - remaining_to_allocate
        report["total_allocated"] += allocated_total

        payment_result = {
            "payment_id": payment.id,
            "store_name": payment.store.name,
            "client_name": payment.client.name,
            "payment_amount": payment.amount or ZERO,
            "allocated_amount": allocated_total,
            "leftover_amount": remaining_to_allocate,
            "allocation_count": len(created_allocations),
        }
        report["payments"].append(payment_result)

        if remaining_to_allocate > ZERO:
            report["unallocated"].append(payment_result)

    return report
