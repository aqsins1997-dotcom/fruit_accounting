from decimal import Decimal

from django.db import models


ZERO = Decimal("0.00")


def repair_supplier_payment_allocations():
    from .models import SupplierPayment, SupplierPaymentAllocation, rebuild_supplier_payment_allocations

    payments = list(
        SupplierPayment.objects.select_related("store", "supplier", "purchase")
        .filter(status=SupplierPayment.STATUS_ACTIVE, allocations__isnull=True)
        .order_by("date", "id")
        .distinct()
    )

    report = {
        "found_count": len(payments),
        "suppliers": [],
        "payments": [],
        "unallocated": [],
        "total_allocated": ZERO,
    }

    if not payments:
        return report

    affected_groups = []
    seen_groups = set()
    seen_suppliers = set()

    for payment in payments:
        supplier_key = (payment.store_id, payment.supplier_id)
        if supplier_key not in seen_suppliers:
            seen_suppliers.add(supplier_key)
            report["suppliers"].append(
                {
                    "store_id": payment.store_id,
                    "store_name": payment.store.name,
                    "supplier_id": payment.supplier_id,
                    "supplier_name": payment.supplier.name,
                }
            )

        group_key = (payment.supplier_id, payment.store_id)
        if group_key not in seen_groups:
            seen_groups.add(group_key)
            affected_groups.append(group_key)

    for supplier_id, store_id in affected_groups:
        rebuild_supplier_payment_allocations(supplier_id=supplier_id, store_id=store_id)

    allocation_totals = {
        row["payment_id"]: row["total_amount"] or ZERO
        for row in SupplierPaymentAllocation.objects.filter(payment_id__in=[payment.id for payment in payments])
        .values("payment_id")
        .annotate(total_amount=models.Sum("amount"))
    }

    for payment in payments:
        allocated_amount = allocation_totals.get(payment.id, ZERO)
        leftover_amount = (payment.amount or ZERO) - allocated_amount
        if leftover_amount < ZERO:
            leftover_amount = ZERO

        payment_result = {
            "payment_id": payment.id,
            "store_name": payment.store.name,
            "supplier_name": payment.supplier.name,
            "payment_amount": payment.amount or ZERO,
            "allocated_amount": allocated_amount,
            "leftover_amount": leftover_amount,
            "allocation_count": payment.allocations.count(),
        }
        report["payments"].append(payment_result)
        report["total_allocated"] += allocated_amount

        if leftover_amount > ZERO:
            report["unallocated"].append(payment_result)

    return report
