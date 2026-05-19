from decimal import Decimal

from django.db import transaction

from apps.inventory.models import PurchaseItem, sync_store_stock_from_active_inventory

from .models import SaleItem, SaleItemBatch, purchase_item_allocated_quantity, purchase_item_available_quantity
from .services import recalculate_sale_costs_for_purchase_item


ZERO_QTY = Decimal("0.000")
ZERO_MONEY = Decimal("0.00")


def _qty(value):
    return (value or ZERO_QTY).quantize(Decimal("0.001"))


def _money(value):
    return (value or ZERO_MONEY).quantize(Decimal("0.01"))


def _active_batches():
    return SaleItemBatch.objects.select_related(
        "sale_item__sale__customer",
        "sale_item__sale__store",
        "sale_item__product",
        "purchase_item__purchase__supplier",
        "purchase_item__store",
        "purchase_item__product",
    ).filter(
        sale_item__sale__deleted_at__isnull=True,
        purchase_item__purchase__deleted_at__isnull=True,
    )


def _sale_order_tuple(sale_item):
    return (
        sale_item.sale.date,
        sale_item.sale_id,
        sale_item.id,
    )


def _batch_row(batch):
    sale_item = batch.sale_item
    sale = sale_item.sale
    return {
        "batch_id": batch.id,
        "sale_item_id": sale_item.id,
        "sale_id": sale.id,
        "sale_date": sale.date,
        "customer": sale.customer.name if sale.customer_id else None,
        "product": sale_item.product.name,
        "store": sale.store.name,
        "sale_quantity": _qty(sale_item.quantity_kg),
        "allocated_quantity": _qty(batch.quantity),
        "line_total": _money(sale_item.line_total),
        "line_cost_total": _money(sale_item.line_cost_total),
        "profit": _money(sale_item.profit),
        "created_at": batch.created_at,
    }


def build_purchase_item_allocation_report(*, purchase_item_id, batch_id=None, reference_sale_item_id=None):
    purchase_item = (
        PurchaseItem.objects.select_related(
            "purchase__supplier",
            "store",
            "product",
        )
        .get(pk=purchase_item_id)
    )
    batches_queryset = _active_batches().filter(purchase_item_id=purchase_item_id)
    if batch_id:
        batches_queryset = batches_queryset.filter(pk=batch_id)
    batches = list(batches_queryset.order_by("sale_item__sale__date", "sale_item_id", "id"))

    sum_allocated = _qty(sum((batch.quantity for batch in batches), ZERO_QTY))
    remaining_stock = _qty(purchase_item.quantity_kg - sum_allocated)
    equation_check = _qty(purchase_item.quantity_kg - sum_allocated - remaining_stock)

    report = {
        "purchase_item_id": purchase_item.id,
        "purchase_id": purchase_item.purchase_id,
        "purchase_date": purchase_item.purchase.date,
        "supplier": purchase_item.purchase.supplier.name,
        "product": purchase_item.product.name,
        "store": purchase_item.store.name,
        "purchase_quantity": _qty(purchase_item.quantity_kg),
        "purchase_price_per_kg": _money(purchase_item.purchase_price_per_kg),
        "purchase_total_cost": _money(purchase_item.total_cost),
        "purchase_remaining_stock": remaining_stock,
        "sum_allocated": sum_allocated,
        "equation_check": equation_check,
        "allocations": [_batch_row(batch) for batch in batches],
        "reference_sale_item_id": reference_sale_item_id,
        "later_allocations": [],
        "later_allocations_total": ZERO_QTY,
    }

    if reference_sale_item_id:
        reference_sale_item = (
            SaleItem.objects.select_related("sale")
            .filter(pk=reference_sale_item_id, sale__deleted_at__isnull=True)
            .first()
        )
        if reference_sale_item:
            reference_order = _sale_order_tuple(reference_sale_item)
            later_batches = [
                batch
                for batch in batches
                if _sale_order_tuple(batch.sale_item) > reference_order
            ]
            report["later_allocations"] = [_batch_row(batch) for batch in later_batches]
            report["later_allocations_total"] = _qty(
                sum((batch.quantity for batch in later_batches), ZERO_QTY)
            )

    return report


def _candidate_purchase_items_for_sale_item(*, sale_item, excluded_purchase_item_id, required_quantity):
    candidates = []
    queryset = (
        PurchaseItem.objects.select_related("purchase", "purchase__supplier", "store", "product")
        .filter(
            purchase__deleted_at__isnull=True,
            purchase__date__lte=sale_item.sale.date,
            store_id=sale_item.sale.store_id,
            product_id=sale_item.product_id,
        )
        .exclude(pk=excluded_purchase_item_id)
        .order_by("purchase__date", "id")
    )
    for purchase_item in queryset:
        available_quantity = purchase_item_available_quantity(purchase_item)
        if available_quantity >= required_quantity:
            candidates.append(
                {
                    "purchase_item": purchase_item,
                    "available_quantity": _qty(available_quantity),
                }
            )
    return candidates


def build_saleitem_batch_mismatch_repair_plan(*, sale_item_id=14, purchase_item_id=None):
    sale_item = (
        SaleItem.objects.select_related("sale__store", "sale__customer", "product")
        .get(pk=sale_item_id, sale__deleted_at__isnull=True)
    )
    active_batches = list(_active_batches().filter(sale_item=sale_item).order_by("id"))
    allocated_quantity = _qty(sum((batch.quantity for batch in active_batches), ZERO_QTY))
    shortfall_quantity = _qty(sale_item.quantity_kg - allocated_quantity)

    plan = {
        "sale_item_id": sale_item.id,
        "sale_id": sale_item.sale_id,
        "sale_date": sale_item.sale.date,
        "customer": sale_item.sale.customer.name if sale_item.sale.customer_id else None,
        "product": sale_item.product.name,
        "store": sale_item.sale.store.name,
        "sale_quantity": _qty(sale_item.quantity_kg),
        "allocated_quantity": allocated_quantity,
        "shortfall_quantity": shortfall_quantity,
        "line_total": _money(sale_item.line_total),
        "line_cost_total": _money(sale_item.line_cost_total),
        "profit": _money(sale_item.profit),
        "repairable": False,
        "reason": "",
        "target_batches": [_batch_row(batch) for batch in active_batches],
        "source_purchase_item_id": None,
        "source_purchase_id": None,
        "candidate_later_batches": [],
        "selected_candidate": None,
        "selected_alternative_purchase_item": None,
    }

    if shortfall_quantity <= ZERO_QTY:
        plan["reason"] = "Sale item does not have an allocation shortfall."
        return plan

    if len(active_batches) != 1:
        plan["reason"] = "Ambiguous target sale item: expected exactly one active batch allocation."
        return plan

    source_batch = active_batches[0]
    source_purchase_item = source_batch.purchase_item
    plan["source_purchase_item_id"] = source_purchase_item.id
    plan["source_purchase_id"] = source_purchase_item.purchase_id

    if purchase_item_id and source_purchase_item.id != purchase_item_id:
        plan["reason"] = (
            f"Target sale item is linked to purchase_item #{source_purchase_item.id}, "
            f"not purchase_item #{purchase_item_id}."
        )
        return plan

    reference_order = _sale_order_tuple(sale_item)
    later_batches = []
    later_queryset = _active_batches().filter(purchase_item=source_purchase_item).exclude(sale_item=sale_item)
    for batch in later_queryset.order_by("sale_item__sale__date", "sale_item_id", "id"):
        if _sale_order_tuple(batch.sale_item) > reference_order:
            later_batches.append(batch)

    plan["candidate_later_batches"] = [_batch_row(batch) for batch in later_batches]
    if not later_batches:
        plan["reason"] = "No later allocations were found on the same purchase batch."
        return plan

    for batch in later_batches:
        candidate_sale_item = batch.sale_item
        candidate_sale_batches = list(_active_batches().filter(sale_item=candidate_sale_item).order_by("id"))
        if len(candidate_sale_batches) != 1 or candidate_sale_batches[0].id != batch.id:
            continue
        if batch.quantity < shortfall_quantity:
            continue

        alternatives = _candidate_purchase_items_for_sale_item(
            sale_item=candidate_sale_item,
            excluded_purchase_item_id=source_purchase_item.id,
            required_quantity=shortfall_quantity,
        )
        if not alternatives:
            continue

        alternative = alternatives[0]
        plan["repairable"] = True
        plan["reason"] = (
            "Safe repair is possible by moving part of a later one-batch sale allocation "
            "to another available purchase batch."
        )
        plan["selected_candidate"] = {
            **_batch_row(batch),
            "new_source_quantity": _qty(batch.quantity - shortfall_quantity),
        }
        plan["selected_alternative_purchase_item"] = {
            "purchase_item_id": alternative["purchase_item"].id,
            "purchase_id": alternative["purchase_item"].purchase_id,
            "purchase_date": alternative["purchase_item"].purchase.date,
            "supplier": alternative["purchase_item"].purchase.supplier.name,
            "available_quantity": alternative["available_quantity"],
            "purchase_price_per_kg": _money(alternative["purchase_item"].purchase_price_per_kg),
        }
        break

    if not plan["repairable"]:
        plan["reason"] = (
            "No safe automatic transfer was found. Either later allocations are too small, "
            "already multi-batch, or no alternative purchase batch has enough available quantity."
        )

    return plan


def _audit_summary():
    from apps.reports.audit import run_accounting_audit

    result = run_accounting_audit()
    return {
        "critical": result["summary"]["critical"],
        "warning": result["summary"]["warning"],
        "info": result["summary"]["info"],
    }


def _raw_remaining_quantity(purchase_item):
    return _qty(purchase_item.quantity_kg - purchase_item_allocated_quantity(purchase_item))


def apply_saleitem_batch_mismatch_repair(*, sale_item_id=14, purchase_item_id=None):
    with transaction.atomic():
        plan = build_saleitem_batch_mismatch_repair_plan(
            sale_item_id=sale_item_id,
            purchase_item_id=purchase_item_id,
        )
        if not plan["repairable"]:
            raise ValueError(plan["reason"])

        sale_item = (
            SaleItem.objects.select_for_update()
            .select_related("sale__store", "product")
            .get(pk=plan["sale_item_id"], sale__deleted_at__isnull=True)
        )
        target_batch = (
            _active_batches()
            .select_for_update()
            .get(sale_item=sale_item, purchase_item_id=plan["source_purchase_item_id"])
        )
        source_purchase_item = (
            PurchaseItem.objects.select_for_update()
            .select_related("purchase", "store", "product")
            .get(pk=plan["source_purchase_item_id"])
        )
        candidate_batch = (
            _active_batches()
            .select_for_update()
            .get(pk=plan["selected_candidate"]["batch_id"])
        )
        candidate_sale_item = (
            SaleItem.objects.select_for_update()
            .select_related("sale__store", "product")
            .get(pk=plan["selected_candidate"]["sale_item_id"], sale__deleted_at__isnull=True)
        )
        alternative_purchase_item = (
            PurchaseItem.objects.select_for_update()
            .select_related("purchase", "store", "product")
            .get(pk=plan["selected_alternative_purchase_item"]["purchase_item_id"])
        )

        shortfall_quantity = plan["shortfall_quantity"]
        available_quantity = purchase_item_available_quantity(alternative_purchase_item)
        if available_quantity < shortfall_quantity:
            raise ValueError(
                "Alternative purchase batch no longer has enough available quantity. "
                f"Available now: {_qty(available_quantity)}."
            )

        target_batch.quantity = sale_item.quantity_kg
        target_batch.total_amount = _money(sale_item.line_total)
        target_batch.sale_price = sale_item.sale_price_per_kg
        target_batch.full_clean()
        target_batch.save(update_fields=["quantity", "sale_price", "total_amount", "updated_at"])

        moved_sale_price = candidate_batch.sale_price
        remaining_source_quantity = _qty(candidate_batch.quantity - shortfall_quantity)
        if remaining_source_quantity <= ZERO_QTY:
            candidate_batch.delete()
        else:
            candidate_batch.quantity = remaining_source_quantity
            candidate_batch.total_amount = _money(remaining_source_quantity * moved_sale_price)
            candidate_batch.full_clean()
            candidate_batch.save(update_fields=["quantity", "total_amount", "updated_at"])

        SaleItemBatch.objects.create(
            sale_item=candidate_sale_item,
            purchase_item=alternative_purchase_item,
            quantity=shortfall_quantity,
            sale_price=moved_sale_price,
            total_amount=_money(shortfall_quantity * moved_sale_price),
        )

        recalculate_sale_costs_for_purchase_item(source_purchase_item)
        recalculate_sale_costs_for_purchase_item(alternative_purchase_item)
        sync_store_stock_from_active_inventory(
            store_id=sale_item.sale.store_id,
            product_id=sale_item.product_id,
        )

        verified_plan = build_saleitem_batch_mismatch_repair_plan(
            sale_item_id=sale_item_id,
            purchase_item_id=source_purchase_item.id,
        )
        if verified_plan["shortfall_quantity"] != ZERO_QTY:
            raise ValueError("Repair verification failed: target sale item still has allocation shortfall.")

        if _raw_remaining_quantity(source_purchase_item) < ZERO_QTY:
            raise ValueError("Repair verification failed: source purchase batch went negative.")
        if _raw_remaining_quantity(alternative_purchase_item) < ZERO_QTY:
            raise ValueError("Repair verification failed: alternative purchase batch went negative.")

        return {
            "before": plan,
            "after": verified_plan,
            "audit_summary": _audit_summary(),
        }
