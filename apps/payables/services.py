from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce


ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _qty(value):
    return (value or ZERO_QTY).quantize(Decimal("0.001"))


def get_purchase_item_sold_quantity(purchase_item):
    from apps.sales.models import SaleItemBatch

    return (
        SaleItemBatch.objects.filter(
            purchase_item=purchase_item,
            purchase_item__purchase__deleted_at__isnull=True,
            sale_item__sale__deleted_at__isnull=True,
        ).aggregate(total=Sum("quantity"))["total"]
        or ZERO_QTY
    )


def build_supplier_purchase_rows(*, supplier_id, store_id, purchase_total_overrides=None):
    from apps.inventory.models import PurchaseItem

    overrides = purchase_total_overrides or {}
    line_total = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )

    rows = list(
        PurchaseItem.objects.filter(
            purchase__supplier_id=supplier_id,
            purchase__deleted_at__isnull=True,
            store_id=store_id,
        )
        .annotate(line_total=line_total)
        .values(
            "purchase_id",
            "purchase__date",
            "purchase__supplier_id",
            "purchase__supplier__name",
            "store_id",
            "store__name",
        )
        .annotate(
            purchase_total=Coalesce(
                Sum("line_total"),
                Value(ZERO, output_field=MONEY_FIELD),
            )
        )
        .order_by("purchase__date", "purchase_id")
    )

    purchase_rows = []
    for row in rows:
        key = (row["purchase_id"], row["store_id"])
        purchase_total = _money(overrides.get(key, row["purchase_total"]))
        purchase_rows.append(
            {
                "purchase_id": row["purchase_id"],
                "purchase_date": row["purchase__date"],
                "supplier_id": row["purchase__supplier_id"],
                "supplier_name": row["purchase__supplier__name"],
                "store_id": row["store_id"],
                "store_name": row["store__name"],
                "purchase_total": purchase_total,
                "remaining_amount": purchase_total,
            }
        )
    return purchase_rows


def simulate_supplier_settlement(*, supplier_id, store_id, purchase_total_overrides=None, exclude_payment_id=None):
    from .models import SupplierPayment

    purchase_rows = build_supplier_purchase_rows(
        supplier_id=supplier_id,
        store_id=store_id,
        purchase_total_overrides=purchase_total_overrides,
    )
    purchases = [dict(row) for row in purchase_rows]
    purchases_by_id = {row["purchase_id"]: row for row in purchases}

    payments = list(
        SupplierPayment.objects.select_related("purchase")
        .filter(
            supplier_id=supplier_id,
            store_id=store_id,
            status=SupplierPayment.STATUS_ACTIVE,
        )
        .order_by("date", "id")
    )
    if exclude_payment_id:
        payments = [payment for payment in payments if payment.id != exclude_payment_id]

    allocations = []
    payment_rows = []
    overpayments = []
    purchase_paid_map = defaultdict(lambda: ZERO)

    for payment in payments:
        remaining_payment = _money(payment.amount)
        payment_allocations = []

        if payment.purchase_id:
            bound_purchase = purchases_by_id.get(payment.purchase_id)
            if bound_purchase and bound_purchase["remaining_amount"] > ZERO:
                applied = min(bound_purchase["remaining_amount"], remaining_payment)
                if applied > ZERO:
                    applied = _money(applied)
                    payment_allocations.append(
                        {
                            "payment_id": payment.id,
                            "purchase_id": payment.purchase_id,
                            "store_id": store_id,
                            "amount": applied,
                        }
                    )
                    purchase_paid_map[(payment.purchase_id, store_id)] += applied
                    bound_purchase["remaining_amount"] = _money(bound_purchase["remaining_amount"] - applied)
                    remaining_payment = _money(remaining_payment - applied)

        if remaining_payment > ZERO:
            for purchase in purchases:
                if remaining_payment <= ZERO:
                    break
                if purchase["remaining_amount"] <= ZERO:
                    continue

                applied = min(purchase["remaining_amount"], remaining_payment)
                if applied <= ZERO:
                    continue

                applied = _money(applied)
                payment_allocations.append(
                    {
                        "payment_id": payment.id,
                        "purchase_id": purchase["purchase_id"],
                        "store_id": store_id,
                        "amount": applied,
                    }
                )
                purchase_paid_map[(purchase["purchase_id"], store_id)] += applied
                purchase["remaining_amount"] = _money(purchase["remaining_amount"] - applied)
                remaining_payment = _money(remaining_payment - applied)

        payment_rows.append(
            {
                "payment_id": payment.id,
                "payment": payment,
                "amount": _money(payment.amount),
                "allocated_total": _money(sum((row["amount"] for row in payment_allocations), ZERO)),
                "leftover_amount": _money(remaining_payment),
                "allocations": payment_allocations,
            }
        )
        allocations.extend(payment_allocations)

        if remaining_payment > ZERO:
            overpayments.append(
                {
                    "payment_id": payment.id,
                    "amount": _money(remaining_payment),
                }
            )

    purchase_result_rows = []
    for purchase in purchase_rows:
        key = (purchase["purchase_id"], purchase["store_id"])
        paid_amount = _money(purchase_paid_map[key])
        remaining_amount = _money(purchase["purchase_total"] - paid_amount)
        if remaining_amount < ZERO:
            remaining_amount = ZERO
        purchase_result_rows.append(
            {
                **purchase,
                "paid_amount": paid_amount,
                "remaining_amount": remaining_amount,
            }
        )

    total_purchase_amount = _money(sum((row["purchase_total"] for row in purchase_result_rows), ZERO))
    total_paid_amount = _money(sum((row["paid_amount"] for row in purchase_result_rows), ZERO))
    total_due_amount = _money(sum((row["remaining_amount"] for row in purchase_result_rows), ZERO))
    total_overpayment = _money(sum((row["amount"] for row in overpayments), ZERO))

    return {
        "supplier_id": supplier_id,
        "store_id": store_id,
        "purchase_rows": purchase_result_rows,
        "payment_rows": payment_rows,
        "allocations": allocations,
        "overpayments": overpayments,
        "purchase_paid_map": {key: _money(value) for key, value in purchase_paid_map.items()},
        "summary": {
            "total_purchase_amount": total_purchase_amount,
            "total_paid_amount": total_paid_amount,
            "total_due_amount": total_due_amount,
            "total_overpayment": total_overpayment,
        },
    }


@transaction.atomic(savepoint=False)
def rebuild_supplier_settlement_state(*, supplier_id, store_id):
    from .models import SupplierOverpayment, SupplierPaymentAllocation

    simulation = simulate_supplier_settlement(supplier_id=supplier_id, store_id=store_id)

    SupplierPaymentAllocation.objects.filter(
        payment__supplier_id=supplier_id,
        payment__store_id=store_id,
        payment__status="active",
    ).delete()
    SupplierOverpayment.objects.filter(
        supplier_id=supplier_id,
        store_id=store_id,
        source_payment__status="active",
    ).delete()

    allocations_to_create = [
        SupplierPaymentAllocation(
            payment_id=row["payment_id"],
            purchase_id=row["purchase_id"],
            store_id=row["store_id"],
            amount=row["amount"],
        )
        for row in simulation["allocations"]
    ]
    if allocations_to_create:
        SupplierPaymentAllocation.objects.bulk_create(allocations_to_create)

    overpayments_to_create = []
    for row in simulation["overpayments"]:
        payment = next(payment_row["payment"] for payment_row in simulation["payment_rows"] if payment_row["payment_id"] == row["payment_id"])
        overpayments_to_create.append(
            SupplierOverpayment(
                supplier_id=supplier_id,
                store_id=store_id,
                source_payment=payment,
                amount=row["amount"],
                remaining_amount=row["amount"],
                comment="Auto-created from unallocated supplier payment balance.",
            )
        )
    if overpayments_to_create:
        SupplierOverpayment.objects.bulk_create(overpayments_to_create)

    return simulation


def calculate_supplier_remaining_debt(*, supplier_id, store_id, exclude_payment_id=None):
    simulation = simulate_supplier_settlement(
        supplier_id=supplier_id,
        store_id=store_id,
        exclude_payment_id=exclude_payment_id,
    )
    return simulation["summary"]["total_due_amount"]


def build_purchase_item_rebalance_preview(*, purchase_item, new_quantity=None, new_unit_price=None):
    sold_quantity = _qty(get_purchase_item_sold_quantity(purchase_item))
    current_quantity = _qty(purchase_item.quantity_kg)
    current_unit_price = _money(purchase_item.purchase_price_per_kg)
    target_quantity = _qty(new_quantity if new_quantity is not None else purchase_item.quantity_kg)
    target_unit_price = _money(new_unit_price if new_unit_price is not None else purchase_item.purchase_price_per_kg)

    if target_quantity < sold_quantity:
        raise ValidationError(
            {
                "quantity_kg": (
                    "Нельзя сделать вес меньше уже проданного по этой партии. "
                    f"Минимум: {sold_quantity} кг."
                )
            }
        )

    current_purchase_total = _money(
        sum(
            (
                item.quantity_kg * item.purchase_price_per_kg
                for item in purchase_item.purchase.items.filter(store=purchase_item.store)
            ),
            ZERO,
        )
    )
    new_purchase_total = _money(
        sum(
            (
                (
                    target_quantity * target_unit_price
                    if item.pk == purchase_item.pk
                    else item.quantity_kg * item.purchase_price_per_kg
                )
                for item in purchase_item.purchase.items.filter(store=purchase_item.store)
            ),
            ZERO,
        )
    )

    purchase_key = (purchase_item.purchase_id, purchase_item.store_id)
    before = simulate_supplier_settlement(
        supplier_id=purchase_item.purchase.supplier_id,
        store_id=purchase_item.store_id,
    )
    after = simulate_supplier_settlement(
        supplier_id=purchase_item.purchase.supplier_id,
        store_id=purchase_item.store_id,
        purchase_total_overrides={purchase_key: new_purchase_total},
    )

    old_allocated_payment = before["purchase_paid_map"].get(purchase_key, ZERO)
    new_allocated_payment = after["purchase_paid_map"].get(purchase_key, ZERO)
    excess_payment = _money(max(ZERO, old_allocated_payment - new_allocated_payment))
    overpayment_created = _money(
        max(
            ZERO,
            after["summary"]["total_overpayment"] - before["summary"]["total_overpayment"],
        )
    )

    destinations = []
    before_paid_map = before["purchase_paid_map"]
    after_paid_map = after["purchase_paid_map"]
    for row in after["purchase_rows"]:
        key = (row["purchase_id"], row["store_id"])
        if key == purchase_key:
            continue
        old_paid = before_paid_map.get(key, ZERO)
        delta_paid = _money(after_paid_map.get(key, ZERO) - old_paid)
        if delta_paid > ZERO:
            destinations.append(
                {
                    "purchase_id": row["purchase_id"],
                    "purchase_date": row["purchase_date"],
                    "store_name": row["store_name"],
                    "supplier_name": row["supplier_name"],
                    "applied_amount": delta_paid,
                    "remaining_amount_after": row["remaining_amount"],
                }
            )

    remaining_stock = _qty(target_quantity - sold_quantity)
    if remaining_stock < ZERO_QTY:
        remaining_stock = ZERO_QTY

    return {
        "purchase_item_id": purchase_item.id,
        "purchase_id": purchase_item.purchase_id,
        "supplier_id": purchase_item.purchase.supplier_id,
        "supplier_name": purchase_item.purchase.supplier.name,
        "store_id": purchase_item.store_id,
        "store_name": purchase_item.store.name,
        "product_name": purchase_item.product.name,
        "old_quantity": current_quantity,
        "new_quantity": target_quantity,
        "old_unit_price": current_unit_price,
        "new_unit_price": target_unit_price,
        "sold_quantity": sold_quantity,
        "remaining_stock": remaining_stock,
        "old_purchase_amount": current_purchase_total,
        "new_purchase_amount": new_purchase_total,
        "old_allocated_payment": _money(old_allocated_payment),
        "new_allocated_payment": _money(new_allocated_payment),
        "excess_payment": excess_payment,
        "redistributions": destinations,
        "overpayment_created": overpayment_created,
        "cash_change": "NO",
        "before_summary": before["summary"],
        "after_summary": after["summary"],
    }


@transaction.atomic(savepoint=False)
def apply_purchase_item_rebalance_update(
    *,
    purchase_item,
    new_quantity=None,
    new_unit_price=None,
    return_preview=False,
):
    preview = None
    if return_preview:
        preview = build_purchase_item_rebalance_preview(
            purchase_item=purchase_item,
            new_quantity=new_quantity,
            new_unit_price=new_unit_price,
        )

    if new_quantity is None and new_unit_price is not None:
        from apps.inventory.models import change_purchase_item_price

        change_purchase_item_price(
            purchase_item=purchase_item,
            new_unit_price=new_unit_price,
        )
    elif new_quantity is not None:
        purchase_item.quantity_kg = new_quantity
        if new_unit_price is not None:
            purchase_item.purchase_price_per_kg = new_unit_price
        purchase_item.save()

    refreshed_preview = None
    if return_preview:
        purchase_item.refresh_from_db()
        refreshed_preview = build_purchase_item_rebalance_preview(purchase_item=purchase_item)
    return {
        "before": preview,
        "after": refreshed_preview,
    }


def build_supplier_rebalance_case_report(*, purchase_item):
    from .models import SupplierOverpayment, SupplierPaymentAllocation
    from apps.sales.services import build_cash_breakdown

    supplier_id = purchase_item.purchase.supplier_id
    store_id = purchase_item.store_id
    purchase_id = purchase_item.purchase_id
    purchase_key = (purchase_id, store_id)

    sold_quantity = _qty(get_purchase_item_sold_quantity(purchase_item))
    remaining_stock = _qty(purchase_item.quantity_kg - sold_quantity)
    if remaining_stock < ZERO_QTY:
        remaining_stock = ZERO_QTY

    purchase_total_amount = _money(
        sum(
            (
                item.quantity_kg * item.purchase_price_per_kg
                for item in purchase_item.purchase.items.filter(store_id=store_id)
            ),
            ZERO,
        )
    )
    line_total_amount = _money(purchase_item.quantity_kg * purchase_item.purchase_price_per_kg)

    simulation = simulate_supplier_settlement(supplier_id=supplier_id, store_id=store_id)
    paid_amount = simulation["purchase_paid_map"].get(purchase_key, ZERO)
    remaining_debt = _money(purchase_total_amount - paid_amount)
    if remaining_debt < ZERO:
        remaining_debt = ZERO

    if remaining_debt == ZERO:
        status = "paid"
    elif paid_amount > ZERO:
        status = "partial"
    else:
        status = "unpaid"

    allocations = list(
        SupplierPaymentAllocation.objects.select_related("payment", "purchase")
        .filter(
            payment__status="active",
            purchase_id=purchase_id,
            store_id=store_id,
        )
        .order_by("payment__date", "payment_id", "id")
    )
    allocation_rows = [
        {
            "allocation_id": allocation.id,
            "payment_id": allocation.payment_id,
            "payment_date": allocation.payment.date,
            "payment_method": allocation.payment.payment_method,
            "payment_amount": _money(allocation.payment.amount),
            "allocated_amount": _money(allocation.amount),
        }
        for allocation in allocations
    ]

    related_payment_ids = {allocation.payment_id for allocation in allocations}
    redistributed_rows = []
    if related_payment_ids:
        sibling_allocations = (
            SupplierPaymentAllocation.objects.select_related("payment", "purchase")
            .filter(payment_id__in=related_payment_ids, payment__status="active")
            .exclude(purchase_id=purchase_id, store_id=store_id)
            .order_by("payment__date", "payment_id", "purchase__date", "purchase_id", "id")
        )
        for allocation in sibling_allocations:
            redistributed_rows.append(
                {
                    "payment_id": allocation.payment_id,
                    "purchase_id": allocation.purchase_id,
                    "purchase_date": allocation.purchase.date,
                    "allocated_amount": _money(allocation.amount),
                }
            )

    supplier_total_paid = simulation["summary"]["total_paid_amount"]
    supplier_total_purchases = simulation["summary"]["total_purchase_amount"]
    supplier_total_debt = simulation["summary"]["total_due_amount"]
    supplier_overpayment = _money(
        SupplierOverpayment.objects.filter(supplier_id=supplier_id, store_id=store_id).aggregate(
            total=models.Sum("remaining_amount")
        )["total"]
        or ZERO
    )

    max_allocation_gap = ZERO
    for row in simulation["purchase_rows"]:
        gap = _money(row["paid_amount"] - row["purchase_total"])
        if gap > max_allocation_gap:
            max_allocation_gap = gap

    return {
        "purchase_item_id": purchase_item.id,
        "purchase_id": purchase_id,
        "supplier_name": purchase_item.purchase.supplier.name,
        "product_name": purchase_item.product.name,
        "store_name": purchase_item.store.name,
        "history_available": False,
        "old_amount": None,
        "new_amount": purchase_total_amount,
        "current_quantity": _qty(purchase_item.quantity_kg),
        "unit_price": _money(purchase_item.purchase_price_per_kg),
        "sold_quantity": sold_quantity,
        "remaining_stock": remaining_stock,
        "line_total_amount": line_total_amount,
        "purchase_total_amount": purchase_total_amount,
        "paid_amount": _money(paid_amount),
        "remaining_debt": remaining_debt,
        "status": status,
        "allocations": allocation_rows,
        "reallocated_targets": redistributed_rows,
        "reallocated_total": _money(sum((row["allocated_amount"] for row in redistributed_rows), ZERO)),
        "supplier_total_purchases": supplier_total_purchases,
        "supplier_total_paid": supplier_total_paid,
        "supplier_total_debt": supplier_total_debt,
        "supplier_overpayment": supplier_overpayment,
        "has_negative_debt": supplier_total_debt < ZERO or remaining_debt < ZERO,
        "allocation_excess_over_purchase": max_allocation_gap,
        "cash_breakdown": build_cash_breakdown(purchase_item.store),
    }


def repair_supplier_payment_allocations():
    from .models import SupplierOverpayment, SupplierPayment, SupplierPaymentAllocation

    payments = list(
        SupplierPayment.objects.select_related("store", "supplier", "purchase")
        .filter(status=SupplierPayment.STATUS_ACTIVE, allocations__isnull=True, overpayment__isnull=True)
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
        rebuild_supplier_settlement_state(supplier_id=supplier_id, store_id=store_id)

    allocation_totals = {
        row["payment_id"]: row["total_amount"] or ZERO
        for row in SupplierPaymentAllocation.objects.filter(payment_id__in=[payment.id for payment in payments])
        .values("payment_id")
        .annotate(total_amount=models.Sum("amount"))
    }
    overpayment_totals = {
        row["source_payment_id"]: row["total_amount"] or ZERO
        for row in SupplierOverpayment.objects.filter(source_payment_id__in=[payment.id for payment in payments])
        .values("source_payment_id")
        .annotate(total_amount=models.Sum("remaining_amount"))
    }

    for payment in payments:
        allocated_amount = allocation_totals.get(payment.id, ZERO)
        leftover_amount = overpayment_totals.get(payment.id, ZERO)

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
