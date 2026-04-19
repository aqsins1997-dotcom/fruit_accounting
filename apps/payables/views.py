from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render

from apps.inventory.models import PurchaseItem

from .models import SupplierPayment


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

    payment_rows = list(
        SupplierPayment.objects.values(
            "id",
            "supplier_id",
            "supplier__name",
            "store_id",
            "store__name",
            "purchase_id",
            "date",
            "amount",
        ).order_by("date", "id")
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

    for payment_row in payment_rows:
        remaining_payment = payment_row["amount"] or Decimal("0.00")
        if remaining_payment <= 0:
            continue

        if payment_row["purchase_id"]:
            purchase_key = (payment_row["purchase_id"], payment_row["store_id"])
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
