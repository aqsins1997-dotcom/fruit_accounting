from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.inventory.models import sync_store_stock_from_active_inventory

from .models import Sale, SaleItem, SaleItemBatch, purchase_item_allocated_quantity, purchase_item_available_quantity


ZERO_QTY = Decimal("0.000")
ZERO_MONEY = Decimal("0.00")


def _money(value):
    return (value or ZERO_MONEY).quantize(Decimal("0.01"))


def _qty(value):
    return (value or ZERO_QTY).quantize(Decimal("0.001"))


def _active_batches_queryset():
    return SaleItemBatch.objects.select_related(
        "sale_item__sale__customer",
        "sale_item__sale__store",
        "sale_item__product",
        "purchase_item__purchase",
        "purchase_item__store",
        "purchase_item__product",
    ).filter(
        sale_item__sale__deleted_at__isnull=True,
        purchase_item__purchase__deleted_at__isnull=True,
    )


def iter_saleitem_allocation_mismatches(*, sale_item_id=None):
    sale_items = SaleItem.objects.select_related(
        "sale__customer",
        "sale__store",
        "product",
    ).filter(sale__deleted_at__isnull=True)
    if sale_item_id:
        sale_items = sale_items.filter(pk=sale_item_id)

    for sale_item in sale_items.order_by("sale__date", "sale_id", "id"):
        batches = list(_active_batches_queryset().filter(sale_item=sale_item).order_by("id"))
        allocated_quantity = _qty(sum((batch.quantity for batch in batches), ZERO_QTY))
        sale_quantity = _qty(sale_item.quantity_kg)
        if allocated_quantity == sale_quantity:
            continue

        yield build_saleitem_forensic_report(
            sale_item=sale_item,
            batches=batches,
            allocated_quantity=allocated_quantity,
        )


def build_saleitem_forensic_report(*, sale_item, batches=None, allocated_quantity=None):
    if batches is None:
        batches = list(_active_batches_queryset().filter(sale_item=sale_item).order_by("id"))
    if allocated_quantity is None:
        allocated_quantity = _qty(sum((batch.quantity for batch in batches), ZERO_QTY))

    batch_rows = []
    for batch in batches:
        purchase_item = batch.purchase_item
        allocated_total = purchase_item_allocated_quantity(purchase_item)
        remaining_stock = purchase_item.quantity_kg - allocated_total
        if remaining_stock < ZERO_QTY:
            remaining_stock = ZERO_QTY

        batch_rows.append(
            {
                "batch_id": batch.id,
                "purchase_item_id": purchase_item.id,
                "purchase_id": purchase_item.purchase_id,
                "purchase_date": purchase_item.purchase.date,
                "purchase_product": purchase_item.product.name,
                "purchase_store": purchase_item.store.name,
                "purchase_quantity": _qty(purchase_item.quantity_kg),
                "purchase_price": _money(purchase_item.purchase_price_per_kg),
                "allocated_quantity": _qty(batch.quantity),
                "purchase_allocated_total": _qty(allocated_total),
                "purchase_remaining_stock": _qty(remaining_stock),
            }
        )

    report = {
        "sale_item_id": sale_item.id,
        "sale_id": sale_item.sale_id,
        "sale_date": sale_item.sale.date,
        "customer": sale_item.sale.customer.name if sale_item.sale.customer_id else None,
        "product": sale_item.product.name,
        "store": sale_item.sale.store.name,
        "sale_quantity": _qty(sale_item.quantity_kg),
        "allocated_quantity": allocated_quantity,
        "sale_price_per_kg": _money(sale_item.sale_price_per_kg),
        "line_total": _money(sale_item.line_total),
        "cost_price_per_kg": _money(sale_item.cost_price_per_kg),
        "line_cost_total": _money(sale_item.line_cost_total),
        "profit": _money(sale_item.profit),
        "batches": batch_rows,
        "reason": "",
        "repairable": False,
        "target_purchase_item_id": None,
        "proposed_quantity": None,
    }

    if not batches:
        report["reason"] = "No active batches are linked to this sale item."
        return report

    if len(batches) != 1:
        report["reason"] = "Ambiguous: more than one active batch is linked to this sale item."
        return report

    purchase_item = batches[0].purchase_item
    available_quantity = purchase_item_available_quantity(
        purchase_item,
        exclude_sale_item_id=sale_item.id,
    )
    if sale_item.quantity_kg > available_quantity:
        report["reason"] = (
            "Insufficient stock in the linked purchase batch to restore full quantity "
            f"without creating negative stock. Available now: {_qty(available_quantity)}."
        )
        return report

    report["repairable"] = True
    report["target_purchase_item_id"] = purchase_item.id
    report["proposed_quantity"] = _qty(sale_item.quantity_kg)
    report["reason"] = (
        "Safe one-batch repair is possible: set the only active batch quantity to the "
        "SaleItem quantity and recalculate cost/profit."
    )
    return report


def apply_saleitem_allocation_repair(*, sale_item_id):
    with transaction.atomic():
        sale_item = (
            SaleItem.objects.select_for_update()
            .select_related("sale__store", "sale__customer", "product")
            .get(pk=sale_item_id, sale__deleted_at__isnull=True)
        )
        batches = list(
            _active_batches_queryset()
            .select_for_update()
            .filter(sale_item=sale_item)
            .order_by("id")
        )
        report = build_saleitem_forensic_report(sale_item=sale_item, batches=batches)
        if not report["repairable"]:
            raise ValueError(report["reason"])

        batch = batches[0]
        purchase_item = batch.purchase_item
        available_quantity = purchase_item_available_quantity(
            purchase_item,
            exclude_sale_item_id=sale_item.id,
        )
        if sale_item.quantity_kg > available_quantity:
            raise ValueError(
                "Repair would create negative stock. "
                f"Available in purchase_item #{purchase_item.id}: {_qty(available_quantity)}."
            )

        batch.quantity = sale_item.quantity_kg
        batch.sale_price = sale_item.sale_price_per_kg
        batch.total_amount = sale_item.line_total
        batch.full_clean()
        batch.save(update_fields=["quantity", "sale_price", "total_amount", "updated_at"])

        total_cost = sale_item.quantity_kg * purchase_item.purchase_price_per_kg
        sale_item.cost_price_per_kg = _money(total_cost / sale_item.quantity_kg)
        sale_item.line_cost_total = _money(total_cost)
        sale_item.profit = _money(sale_item.line_total - sale_item.line_cost_total)
        sale_item.save(
            update_fields=[
                "cost_price_per_kg",
                "line_cost_total",
                "profit",
                "updated_at",
            ]
        )

        sync_store_stock_from_active_inventory(
            store_id=sale_item.sale.store_id,
            product_id=sale_item.product_id,
        )

        sale = Sale.objects.select_for_update().get(pk=sale_item.sale_id)
        sale.recalculate_totals()

        verified = build_saleitem_forensic_report(sale_item=sale_item)
        if not verified["repairable"] and verified["allocated_quantity"] != verified["sale_quantity"]:
            raise ValueError("Repair verification failed: mismatch still exists after update.")

        final_available = purchase_item_available_quantity(purchase_item)
        if final_available < ZERO_QTY:
            raise ValueError("Repair verification failed: negative stock detected.")

        return verified
