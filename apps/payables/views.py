from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render

from apps.inventory.models import PurchaseItem
from .models import SupplierPayment


@login_required
def supplier_balances(request):
    purchase_rows = (
        PurchaseItem.objects
        .values(
            "store_id",
            "store__name",
            "purchase__supplier_id",
            "purchase__supplier__name",
        )
        .annotate(
            purchases_total=Sum("total_cost")
        )
        .order_by("store__name", "purchase__supplier__name")
    )

    payment_rows = (
        SupplierPayment.objects
        .values(
            "store_id",
            "store__name",
            "supplier_id",
            "supplier__name",
        )
        .annotate(
            payments_total=Sum("amount")
        )
        .order_by("store__name", "supplier__name")
    )

    combined = {}

    for row in purchase_rows:
        key = (row["store_id"], row["purchase__supplier_id"])
        combined[key] = {
            "store_name": row["store__name"],
            "supplier_name": row["purchase__supplier__name"],
            "purchases_total": row["purchases_total"] or Decimal("0.00"),
            "payments_total": Decimal("0.00"),
        }

    for row in payment_rows:
        key = (row["store_id"], row["supplier_id"])
        if key not in combined:
            combined[key] = {
                "store_name": row["store__name"],
                "supplier_name": row["supplier__name"],
                "purchases_total": Decimal("0.00"),
                "payments_total": Decimal("0.00"),
            }

        combined[key]["payments_total"] = row["payments_total"] or Decimal("0.00")

    rows = []
    total_purchases = Decimal("0.00")
    total_payments = Decimal("0.00")
    total_due = Decimal("0.00")

    for item in combined.values():
        due_amount = item["purchases_total"] - item["payments_total"]
        rows.append({
            "store_name": item["store_name"],
            "supplier_name": item["supplier_name"],
            "purchases_total": item["purchases_total"],
            "payments_total": item["payments_total"],
            "due_amount": due_amount,
        })
        total_purchases += item["purchases_total"]
        total_payments += item["payments_total"]
        total_due += due_amount

    rows.sort(key=lambda x: (x["store_name"], x["supplier_name"]))

    context = {
        "rows": rows,
        "summary": {
            "total_purchases": total_purchases,
            "total_payments": total_payments,
            "total_due": total_due,
        }
    }
    return render(request, "payables/supplier_balances.html", context)