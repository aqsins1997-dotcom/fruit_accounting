from collections import defaultdict
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce

ZERO_MONEY = Decimal("0.00")
ZERO_QUANTITY = Decimal("0.000")
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
QUANTITY_FIELD = DecimalField(max_digits=14, decimal_places=3)


def _money(value):
    return (value or ZERO_MONEY).quantize(Decimal("0.01"))


def _quantity(value):
    return (value or ZERO_QUANTITY).quantize(Decimal("0.001"))


def _divide_money(amount, quantity):
    if not quantity or quantity <= ZERO_QUANTITY:
        return ZERO_MONEY
    return _money(amount / quantity)


def _base_row():
    return {
        "store_id": None,
        "store_name": "Все магазины",
        "product_id": None,
        "product_name": "",
        "purchased_quantity": ZERO_QUANTITY,
        "purchase_cost": ZERO_MONEY,
        "sold_quantity": ZERO_QUANTITY,
        "revenue": ZERO_MONEY,
        "sold_cost": ZERO_MONEY,
        "stock_quantity": ZERO_QUANTITY,
        "stock_cost": ZERO_MONEY,
    }


def _row_key(row, *, group_by_store, store_prefix="", product_prefix=""):
    if group_by_store:
        return (
            row[f"{store_prefix}store_id"],
            row[f"{product_prefix}product_id"],
        )
    return (None, row[f"{product_prefix}product_id"])


def _set_identity(target, row, *, group_by_store, store_prefix="", product_prefix=""):
    target["product_id"] = row[f"{product_prefix}product_id"]
    target["product_name"] = row[f"{product_prefix}product__name"]
    if group_by_store:
        target["store_id"] = row[f"{store_prefix}store_id"]
        target["store_name"] = row[f"{store_prefix}store__name"]


def build_product_profitability_rows(
    *,
    store=None,
    product=None,
    date_from=None,
    date_to=None,
    group_by_store=True,
):
    from apps.inventory.models import PurchaseItem
    from apps.sales.models import SaleItem, SaleItemBatch

    rows = defaultdict(_base_row)

    purchase_values = ["product_id", "product__name"]
    sale_values = ["product_id", "product__name"]
    if group_by_store:
        purchase_values = ["store_id", "store__name", *purchase_values]
        sale_values = ["sale__store_id", "sale__store__name", *sale_values]

    purchase_total_expr = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )
    purchase_queryset = PurchaseItem.objects.select_related("purchase", "store", "product").filter(
        purchase__deleted_at__isnull=True
    )
    if store:
        purchase_queryset = purchase_queryset.filter(store=store)
    if product:
        purchase_queryset = purchase_queryset.filter(product=product)
    if date_from:
        purchase_queryset = purchase_queryset.filter(purchase__date__gte=date_from)
    if date_to:
        purchase_queryset = purchase_queryset.filter(purchase__date__lte=date_to)

    for row in purchase_queryset.values(*purchase_values).annotate(
        purchased_quantity=Coalesce(
            Sum("quantity_kg"),
            Value(ZERO_QUANTITY, output_field=QUANTITY_FIELD),
            output_field=QUANTITY_FIELD,
        ),
        purchase_cost=Coalesce(
            Sum(purchase_total_expr),
            Value(ZERO_MONEY, output_field=MONEY_FIELD),
            output_field=MONEY_FIELD,
        ),
    ):
        target = rows[_row_key(row, group_by_store=group_by_store)]
        _set_identity(target, row, group_by_store=group_by_store)
        target["purchased_quantity"] = _quantity(row["purchased_quantity"])
        target["purchase_cost"] = _money(row["purchase_cost"])

    sale_queryset = SaleItem.objects.select_related("sale", "sale__store", "product").filter(
        sale__deleted_at__isnull=True
    )
    if store:
        sale_queryset = sale_queryset.filter(sale__store=store)
    if product:
        sale_queryset = sale_queryset.filter(product=product)
    if date_from:
        sale_queryset = sale_queryset.filter(sale__date__gte=date_from)
    if date_to:
        sale_queryset = sale_queryset.filter(sale__date__lte=date_to)

    for row in sale_queryset.values(*sale_values).annotate(
        sold_quantity=Coalesce(
            Sum("quantity_kg"),
            Value(ZERO_QUANTITY, output_field=QUANTITY_FIELD),
            output_field=QUANTITY_FIELD,
        ),
        revenue=Coalesce(
            Sum("line_total"),
            Value(ZERO_MONEY, output_field=MONEY_FIELD),
            output_field=MONEY_FIELD,
        ),
    ):
        target = rows[_row_key(row, group_by_store=group_by_store, store_prefix="sale__")]
        _set_identity(target, row, group_by_store=group_by_store, store_prefix="sale__")
        target["sold_quantity"] = _quantity(row["sold_quantity"])
        target["revenue"] = _money(row["revenue"])

    batch_cost_values = ["sale_item__product_id", "sale_item__product__name"]
    if group_by_store:
        batch_cost_values = ["sale_item__sale__store_id", "sale_item__sale__store__name", *batch_cost_values]

    batch_cost_expr = ExpressionWrapper(
        F("quantity") * F("purchase_item__purchase_price_per_kg"),
        output_field=MONEY_FIELD,
    )
    batch_cost_queryset = SaleItemBatch.objects.select_related(
        "sale_item__sale",
        "sale_item__product",
        "purchase_item__purchase",
    ).filter(
        sale_item__sale__deleted_at__isnull=True,
        purchase_item__purchase__deleted_at__isnull=True,
    )
    if store:
        batch_cost_queryset = batch_cost_queryset.filter(sale_item__sale__store=store)
    if product:
        batch_cost_queryset = batch_cost_queryset.filter(sale_item__product=product)
    if date_from:
        batch_cost_queryset = batch_cost_queryset.filter(sale_item__sale__date__gte=date_from)
    if date_to:
        batch_cost_queryset = batch_cost_queryset.filter(sale_item__sale__date__lte=date_to)

    for row in batch_cost_queryset.values(*batch_cost_values).annotate(
        sold_cost=Coalesce(
            Sum(batch_cost_expr),
            Value(ZERO_MONEY, output_field=MONEY_FIELD),
            output_field=MONEY_FIELD,
        ),
    ):
        target = rows[
            _row_key(
                row,
                group_by_store=group_by_store,
                store_prefix="sale_item__sale__",
                product_prefix="sale_item__",
            )
        ]
        _set_identity(
            target,
            row,
            group_by_store=group_by_store,
            store_prefix="sale_item__sale__",
            product_prefix="sale_item__",
        )
        target["sold_cost"] = _money(row["sold_cost"])

    completed_rows = []
    for row in rows.values():
        average_purchase_price = _divide_money(row["purchase_cost"], row["purchased_quantity"])
        if average_purchase_price == ZERO_MONEY:
            average_purchase_price = _divide_money(row["sold_cost"], row["sold_quantity"])

        average_sale_price = _divide_money(row["revenue"], row["sold_quantity"])
        sold_cost = row["sold_cost"]

        profit = _money(row["revenue"] - sold_cost)
        margin_per_unit = ZERO_MONEY
        if row["sold_quantity"] > ZERO_QUANTITY:
            margin_per_unit = _money(average_sale_price - average_purchase_price)

        stock_quantity = row["purchased_quantity"] - row["sold_quantity"]
        if stock_quantity < ZERO_QUANTITY:
            stock_quantity = ZERO_QUANTITY
        stock_cost = _money(stock_quantity * average_purchase_price)

        completed_rows.append(
            {
                **row,
                "stock_quantity": _quantity(stock_quantity),
                "stock_cost": stock_cost,
                "average_purchase_price": average_purchase_price,
                "average_sale_price": average_sale_price,
                "sold_cost": sold_cost,
                "profit": profit,
                "margin_per_unit": margin_per_unit,
            }
        )

    completed_rows.sort(key=lambda item: (item["store_name"], item["product_name"]))
    return completed_rows


def summarize_product_profitability(rows):
    purchased_quantity = _quantity(sum((row["purchased_quantity"] for row in rows), ZERO_QUANTITY))
    sold_quantity = _quantity(sum((row["sold_quantity"] for row in rows), ZERO_QUANTITY))
    revenue = _money(sum((row["revenue"] for row in rows), ZERO_MONEY))
    sold_cost = _money(sum((row["sold_cost"] for row in rows), ZERO_MONEY))
    profit = _money(sum((row["profit"] for row in rows), ZERO_MONEY))

    return {
        "purchased_quantity": purchased_quantity,
        "sold_quantity": sold_quantity,
        "stock_quantity": _quantity(sum((row["stock_quantity"] for row in rows), ZERO_QUANTITY)),
        "average_purchase_price": _divide_money(
            sum((row["purchase_cost"] for row in rows), ZERO_MONEY),
            purchased_quantity,
        ),
        "average_sale_price": _divide_money(revenue, sold_quantity),
        "revenue": revenue,
        "sold_cost": sold_cost,
        "profit": profit,
        "margin_per_unit": _divide_money(profit, sold_quantity),
    }


def build_purchase_item_profitability_map(*, purchase_item_ids=None, date_from=None, date_to=None):
    from apps.inventory.models import PurchaseItem
    from apps.sales.models import SaleItemBatch

    purchase_items = PurchaseItem.objects.select_related("purchase", "store", "product").filter(
        purchase__deleted_at__isnull=True
    )
    if purchase_item_ids is not None:
        purchase_items = purchase_items.filter(id__in=purchase_item_ids)

    purchase_item_map = {item.id: item for item in purchase_items}
    batch_queryset = SaleItemBatch.objects.filter(
        purchase_item_id__in=purchase_item_map.keys(),
        purchase_item__purchase__deleted_at__isnull=True,
        sale_item__sale__deleted_at__isnull=True,
    )
    if date_from:
        batch_queryset = batch_queryset.filter(sale_item__sale__date__gte=date_from)
    if date_to:
        batch_queryset = batch_queryset.filter(sale_item__sale__date__lte=date_to)

    batch_totals = {
        row["purchase_item_id"]: row
        for row in batch_queryset.values("purchase_item_id").annotate(
            sold_quantity=Coalesce(
                Sum("quantity"),
                Value(ZERO_QUANTITY, output_field=QUANTITY_FIELD),
                output_field=QUANTITY_FIELD,
            ),
            revenue=Coalesce(
                Sum("total_amount"),
                Value(ZERO_MONEY, output_field=MONEY_FIELD),
                output_field=MONEY_FIELD,
            ),
        )
    }

    rows = {}
    for item_id, item in purchase_item_map.items():
        totals = batch_totals.get(item_id, {})
        sold_quantity = _quantity(totals.get("sold_quantity", ZERO_QUANTITY))
        revenue = _money(totals.get("revenue", ZERO_MONEY))
        average_sale_price = _divide_money(revenue, sold_quantity)
        sold_cost = _money(sold_quantity * item.purchase_price_per_kg)
        profit = _money(revenue - sold_cost)
        margin_per_unit = ZERO_MONEY
        if sold_quantity > ZERO_QUANTITY:
            margin_per_unit = _money(average_sale_price - item.purchase_price_per_kg)

        stock_quantity = item.quantity_kg - sold_quantity
        if stock_quantity < ZERO_QUANTITY:
            stock_quantity = ZERO_QUANTITY

        rows[item_id] = {
            "purchase_item_id": item.id,
            "store_id": item.store_id,
            "store_name": item.store.name,
            "product_id": item.product_id,
            "product_name": item.product.name,
            "purchased_quantity": _quantity(item.quantity_kg),
            "purchase_price": _money(item.purchase_price_per_kg),
            "sold_quantity": sold_quantity,
            "stock_quantity": _quantity(stock_quantity),
            "average_purchase_price": _money(item.purchase_price_per_kg),
            "average_sale_price": average_sale_price,
            "revenue": revenue,
            "sold_cost": sold_cost,
            "profit": profit,
            "margin_per_unit": margin_per_unit,
        }

    return rows
