from decimal import Decimal
from functools import lru_cache

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.inventory.models import PurchaseItem

from .forms import SupplierPaymentCreateForm
from .models import SupplierPayment, SupplierPaymentAllocation


@lru_cache(maxsize=None)
def _table_columns(table_name):
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table_name)
    return {column.name for column in description}


@lru_cache(maxsize=1)
def _existing_tables():
    return set(connection.introspection.table_names())


@login_required
def supplier_payment_create(request):
    if request.method == "POST":
        form = SupplierPaymentCreateForm(request.POST)
        if form.is_valid():
            payment = form.save()
            messages.success(
                request,
                f"Оплата поставщику сохранена: {payment.supplier} / {payment.amount}.",
            )
            return redirect("payables:supplier_balances")
    else:
        form = SupplierPaymentCreateForm(initial={"date": timezone.now().date()})

    recent_payments = SupplierPayment.objects.select_related(
        "supplier",
        "store",
        "purchase",
    )[:10]

    context = {
        "form": form,
        "recent_payments": recent_payments,
    }
    return render(request, "payables/supplier_payment_form.html", context)


@login_required
def supplier_balances(request):
    money_field = DecimalField(max_digits=14, decimal_places=2)
    line_total = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=money_field,
    )

    purchase_rows = list(
        PurchaseItem.objects.annotate(line_total=line_total)
        .values(
            "purchase_id",
            "purchase__date",
            "store_id",
            "store__name",
            "purchase__supplier_id",
            "purchase__supplier__name",
        )
        .annotate(
            purchase_total=Coalesce(
                Sum("line_total"),
                Value(Decimal("0.00"), output_field=money_field),
            )
        )
        .order_by("purchase__supplier__name", "store__name", "purchase__date", "purchase_id")
    )

    rows = []
    purchase_lookup = {}
    rows_by_group = {}

    for purchase_row in purchase_rows:
        report_row = {
            "purchase_id": purchase_row["purchase_id"],
            "purchase_date": purchase_row["purchase__date"],
            "store_id": purchase_row["store_id"],
            "store_name": purchase_row["store__name"],
            "supplier_id": purchase_row["purchase__supplier_id"],
            "supplier_name": purchase_row["purchase__supplier__name"],
            "purchase_total": purchase_row["purchase_total"] or Decimal("0.00"),
            "paid_amount": Decimal("0.00"),
            "remaining_amount": purchase_row["purchase_total"] or Decimal("0.00"),
            "status": "Не оплачено",
            "status_class": "badge--danger",
        }
        rows.append(report_row)
        purchase_lookup[(report_row["purchase_id"], report_row["store_id"])] = report_row
        rows_by_group.setdefault((report_row["supplier_id"], report_row["store_id"]), []).append(report_row)

    allocation_table_exists = SupplierPaymentAllocation._meta.db_table in _existing_tables()
    if allocation_table_exists:
        allocation_columns = _table_columns(SupplierPaymentAllocation._meta.db_table)
    else:
        allocation_columns = set()

    if {"payment_id", "purchase_id", "store_id", "amount"}.issubset(allocation_columns):
        allocation_rows = SupplierPaymentAllocation.objects.values(
            "payment_id",
            "purchase_id",
            "store_id",
            "amount",
        )
        for allocation_row in allocation_rows:
            target_row = purchase_lookup.get((allocation_row["purchase_id"], allocation_row["store_id"]))
            if not target_row:
                continue
            applied = allocation_row["amount"] or Decimal("0.00")
            target_row["paid_amount"] += applied
            target_row["remaining_amount"] -= applied
    else:
        payment_value_fields = [
            "id",
            "supplier_id",
            "supplier__name",
            "store_id",
            "store__name",
            "date",
            "amount",
        ]
        payment_columns = _table_columns(SupplierPayment._meta.db_table)
        if "purchase_id" in payment_columns:
            payment_value_fields.append("purchase_id")

        payment_rows = list(
            SupplierPayment.objects.values(*payment_value_fields).order_by("date", "id")
        )

        for payment_row in payment_rows:
            remaining_payment = payment_row["amount"] or Decimal("0.00")
            if remaining_payment <= 0:
                continue

            purchase_id = payment_row.get("purchase_id")
            if purchase_id:
                purchase_key = (purchase_id, payment_row["store_id"])
                target_row = purchase_lookup.get(purchase_key)
                if target_row:
                    applied = min(target_row["remaining_amount"], remaining_payment)
                    target_row["paid_amount"] += applied
                    target_row["remaining_amount"] -= applied
                    remaining_payment -= applied

            if remaining_payment > 0:
                group_key = (payment_row["supplier_id"], payment_row["store_id"])
                for target_row in rows_by_group.get(group_key, []):
                    if remaining_payment <= 0:
                        break
                    if target_row["remaining_amount"] <= 0:
                        continue

                    applied = min(target_row["remaining_amount"], remaining_payment)
                    target_row["paid_amount"] += applied
                    target_row["remaining_amount"] -= applied
                    remaining_payment -= applied

    supplier_groups_map = {}
    total_purchases = Decimal("0.00")
    total_payments = Decimal("0.00")
    total_due = Decimal("0.00")

    for row in rows:
        if row["remaining_amount"] <= 0:
            row["remaining_amount"] = Decimal("0.00")
            row["status"] = "Оплачено"
            row["status_class"] = "badge--success"
        elif row["paid_amount"] > 0:
            row["status"] = "Частично оплачено"
            row["status_class"] = "badge--warning"

        total_purchases += row["purchase_total"]
        total_payments += row["paid_amount"]
        total_due += row["remaining_amount"]

        supplier_group = supplier_groups_map.setdefault(
            row["supplier_id"],
            {
                "supplier_id": row["supplier_id"],
                "supplier_name": row["supplier_name"],
                "purchase_total": Decimal("0.00"),
                "paid_amount": Decimal("0.00"),
                "remaining_amount": Decimal("0.00"),
                "rows": [],
            },
        )
        supplier_group["purchase_total"] += row["purchase_total"]
        supplier_group["paid_amount"] += row["paid_amount"]
        supplier_group["remaining_amount"] += row["remaining_amount"]
        supplier_group["rows"].append(row)

    supplier_groups = sorted(
        supplier_groups_map.values(),
        key=lambda group: (group["supplier_name"], group["supplier_id"]),
    )

    context = {
        "supplier_groups": supplier_groups,
        "summary": {
            "total_purchases": total_purchases,
            "total_payments": total_payments,
            "total_due": total_due,
        },
    }
    return render(request, "payables/supplier_balances.html", context)
