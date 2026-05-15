from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Q, Sum

from apps.core.models import Product, Store
from apps.inventory.models import PurchaseItem
from apps.payables.models import SupplierPaymentAllocation
from apps.sales.models import Sale, SaleItem, SaleItemBatch


ZERO_QTY = Decimal("0.000")
ZERO_MONEY = Decimal("0.00")


def q(value):
    return (value or ZERO_QTY).quantize(Decimal("0.001"))


def m(value):
    return (value or ZERO_MONEY).quantize(Decimal("0.01"))


def fmt(value):
    if value is None:
        return "-"
    return str(value)


class Command(BaseCommand):
    help = "Read-only FIFO, stock, margin and supplier-payment diagnostics for one product/store."

    def add_arguments(self, parser):
        parser.add_argument("--product", required=True, help="Product name or name fragment.")
        parser.add_argument("--store", required=True, help="Store name or name fragment.")
        parser.add_argument(
            "--suspect-amount",
            default="15900.00",
            help="Sale total amount to highlight. Default: 15900.00.",
        )
        parser.add_argument(
            "--expected-visible-kg",
            default=None,
            help="Optional kg total from visual/manual check, used to highlight the delta.",
        )

    def handle(self, *args, **options):
        product = self._find_one(Product, options["product"], "product")
        store = self._find_one(Store, options["store"], "store")
        suspect_amount = Decimal(str(options["suspect_amount"]))
        expected_visible_kg = (
            Decimal(str(options["expected_visible_kg"]))
            if options["expected_visible_kg"] is not None
            else None
        )

        self.stdout.write("READ ONLY DIAGNOSTIC: no data will be changed.")
        self.stdout.write(f"Product: #{product.id} {product.name}")
        self.stdout.write(f"Store:   #{store.id} {store.name}")
        self.stdout.write("")

        purchase_items = list(
            PurchaseItem.objects.select_related("purchase", "purchase__supplier", "store", "product")
            .filter(store=store, product=product, purchase__deleted_at__isnull=True)
            .order_by("purchase__date", "purchase_id", "id")
        )
        purchase_item_ids = [item.id for item in purchase_items]
        purchase_ids = sorted({item.purchase_id for item in purchase_items})

        sale_items = list(
            SaleItem.objects.select_related("sale", "sale__customer", "sale__store", "product")
            .filter(sale__store=store, product=product, sale__deleted_at__isnull=True)
            .order_by("sale__date", "sale_id", "id")
        )
        sale_ids = sorted({item.sale_id for item in sale_items})

        batches = list(
            SaleItemBatch.objects.select_related(
                "sale_item",
                "sale_item__sale",
                "sale_item__sale__customer",
                "sale_item__product",
                "purchase_item",
                "purchase_item__purchase",
                "purchase_item__purchase__supplier",
                "purchase_item__store",
                "purchase_item__product",
            )
            .filter(
                Q(purchase_item__store=store, purchase_item__product=product)
                | Q(sale_item__sale__store=store, sale_item__product=product)
            )
            .order_by("purchase_item__purchase__date", "purchase_item_id", "sale_item__sale__date", "id")
        )

        active_batches = [
            batch
            for batch in batches
            if self._batch_active_reason(batch) == "active"
        ]

        self._print_purchases(purchase_items)
        self._print_sales(sale_items)
        self._print_sales_checks(
            store=store,
            product=product,
            purchase_items=purchase_items,
            sale_items=sale_items,
            active_batches=active_batches,
            suspect_amount=suspect_amount,
            expected_visible_kg=expected_visible_kg,
        )
        self._print_batches(batches)
        self._print_purchase_groups(purchase_items, active_batches)
        self._print_supplier_payment_allocations(store=store, purchase_ids=purchase_ids)
        self._print_orphans()

    def _find_one(self, model, term, label):
        matches = list(model.objects.filter(name__icontains=term).order_by("name")[:10])
        if not matches:
            raise CommandError(f"No {label} found for: {term}")
        if len(matches) > 1:
            self.stdout.write(f"Multiple {label}s matched {term!r}; using first:")
            for obj in matches:
                self.stdout.write(f"  #{obj.id} {obj.name}")
        return matches[0]

    def _print_purchases(self, purchase_items):
        self.stdout.write("A. ACTIVE PURCHASES")
        if not purchase_items:
            self.stdout.write("  No active purchases found.")
        total_qty = ZERO_QTY
        total_amount = ZERO_MONEY
        for item in purchase_items:
            line_total = m(item.quantity_kg * item.purchase_price_per_kg)
            total_qty += item.quantity_kg
            total_amount += line_total
            self.stdout.write(
                "  "
                f"purchase=#{item.purchase_id} date={item.purchase.date} store={item.store.name} "
                f"supplier={item.purchase.supplier.name} deleted_at={fmt(item.purchase.deleted_at)} "
                f"item=#{item.id} qty={q(item.quantity_kg)} price={m(item.purchase_price_per_kg)} total={line_total}"
            )
        self.stdout.write(f"  TOTAL purchased_qty={q(total_qty)} purchase_amount={m(total_amount)}")
        self.stdout.write("")

    def _print_sales(self, sale_items):
        self.stdout.write("B. ACTIVE SALES ITEMS")
        if not sale_items:
            self.stdout.write("  No active sale items found.")
        total_qty = ZERO_QTY
        total_amount = ZERO_MONEY
        for item in sale_items:
            total_qty += item.quantity_kg or ZERO_QTY
            total_amount += item.line_total or ZERO_MONEY
            sale = item.sale
            customer = sale.customer.name if sale.customer else "-"
            self.stdout.write(
                "  "
                f"sale=#{sale.id} date={sale.date} payment={sale.payment_type} client={customer} "
                f"deleted_at={fmt(sale.deleted_at)} sale_total={m(sale.total_amount)} "
                f"item=#{item.id} product={item.product.name} qty={q(item.quantity_kg)} "
                f"price={m(item.sale_price_per_kg)} line_total={m(item.line_total)}"
            )
        self.stdout.write(f"  TOTAL sale_item_qty={q(total_qty)} sale_item_amount={m(total_amount)}")
        self.stdout.write("")

    def _print_sales_checks(
        self,
        *,
        store,
        product,
        purchase_items,
        sale_items,
        active_batches,
        suspect_amount,
        expected_visible_kg,
    ):
        self.stdout.write("C. SALES CHECKS")

        sale_item_qty = sum((item.quantity_kg or ZERO_QTY for item in sale_items), ZERO_QTY)
        batch_qty = sum((batch.quantity or ZERO_QTY for batch in active_batches), ZERO_QTY)
        purchased_qty = sum((item.quantity_kg or ZERO_QTY for item in purchase_items), ZERO_QTY)
        self.stdout.write(f"  active SaleItem kg total:       {q(sale_item_qty)}")
        self.stdout.write(f"  active FIFO allocation kg total:{q(batch_qty)}")
        self.stdout.write(f"  SaleItem - FIFO difference:     {q(sale_item_qty - batch_qty)}")
        self.stdout.write(f"  stock by SaleItem formula:      {q(purchased_qty - sale_item_qty)}")
        self.stdout.write(f"  stock by FIFO formula:          {q(purchased_qty - batch_qty)}")
        if expected_visible_kg is not None:
            self.stdout.write(f"  expected visible kg:            {q(expected_visible_kg)}")
            self.stdout.write(f"  SaleItem - expected difference: {q(sale_item_qty - expected_visible_kg)}")
            self.stdout.write(f"  FIFO - expected difference:     {q(batch_qty - expected_visible_kg)}")

        allocated_by_sale_item = defaultdict(Decimal)
        for batch in active_batches:
            allocated_by_sale_item[batch.sale_item_id] += batch.quantity or ZERO_QTY

        mismatches = []
        for item in sale_items:
            allocated_qty = allocated_by_sale_item.get(item.id, ZERO_QTY)
            if q(item.quantity_kg) != q(allocated_qty):
                mismatches.append((item, allocated_qty))
        self.stdout.write("  SaleItem/FIFO mismatches:")
        if mismatches:
            for item, allocated_qty in mismatches:
                self.stdout.write(
                    f"    sale=#{item.sale_id} item=#{item.id} item_qty={q(item.quantity_kg)} "
                    f"allocated_qty={q(allocated_qty)} diff={q((item.quantity_kg or ZERO_QTY) - allocated_qty)}"
                )
        else:
            self.stdout.write("    none")

        sales_without_items = (
            Sale.objects.filter(store=store, deleted_at__isnull=True, total_amount__gt=0)
            .filter(items__isnull=True)
            .order_by("date", "id")
        )
        self.stdout.write("  Active sales with total_amount > 0 but without SaleItem:")
        found = False
        for sale in sales_without_items:
            found = True
            self.stdout.write(
                f"    sale=#{sale.id} date={sale.date} payment={sale.payment_type} total={m(sale.total_amount)}"
            )
        if not found:
            self.stdout.write("    none")

        suspect_sales = (
            Sale.objects.filter(store=store, deleted_at__isnull=True, total_amount=suspect_amount)
            .prefetch_related("items__product")
            .order_by("date", "id")
        )
        self.stdout.write(f"  Active sales with total_amount={m(suspect_amount)}:")
        found = False
        for sale in suspect_sales:
            found = True
            matching_items = [item for item in sale.items.all() if item.product_id == product.id]
            all_items = list(sale.items.all())
            if matching_items:
                for item in matching_items:
                    self.stdout.write(
                        f"    sale=#{sale.id} item=#{item.id} product={item.product.name} "
                        f"qty={q(item.quantity_kg)} line_total={m(item.line_total)}"
                    )
            elif all_items:
                self.stdout.write(
                    f"    sale=#{sale.id} has items, but none for product {product.name}: "
                    + ", ".join(f"{item.product.name}/{q(item.quantity_kg)}kg" for item in all_items)
                )
            else:
                self.stdout.write(f"    sale=#{sale.id} has no SaleItem rows")
        if not found:
            self.stdout.write("    none")

        zero_or_empty = [
            item
            for item in sale_items
            if item.quantity_kg is None or item.quantity_kg <= ZERO_QTY or not item.product_id
        ]
        self.stdout.write("  Active sale items with empty/zero product or quantity:")
        if zero_or_empty:
            for item in zero_or_empty:
                self.stdout.write(
                    f"    sale=#{item.sale_id} item=#{item.id} product_id={item.product_id} qty={fmt(item.quantity_kg)}"
                )
        else:
            self.stdout.write("    none")
        self.stdout.write("")

    def _batch_active_reason(self, batch):
        if not batch.sale_item_id:
            return "ignored: missing sale item id"
        if not batch.purchase_item_id:
            return "ignored: missing purchase item id"
        sale = batch.sale_item.sale
        purchase = batch.purchase_item.purchase
        if sale.deleted_at:
            return "ignored: sale soft-deleted"
        if purchase.deleted_at:
            return "ignored: purchase soft-deleted"
        return "active"

    def _print_batches(self, batches):
        self.stdout.write("D. FIFO ALLOCATIONS")
        if not batches:
            self.stdout.write("  No FIFO allocations found.")
        for batch in batches:
            sale = batch.sale_item.sale
            purchase = batch.purchase_item.purchase
            reason = self._batch_active_reason(batch)
            allocated_cost = m((batch.quantity or ZERO_QTY) * batch.purchase_item.purchase_price_per_kg)
            self.stdout.write(
                "  "
                f"allocation=#{batch.id} sale=#{sale.id} sale_deleted_at={fmt(sale.deleted_at)} "
                f"sale_item=#{batch.sale_item_id} sale_item_qty={q(batch.sale_item.quantity_kg)} "
                f"purchase=#{purchase.id} purchase_deleted_at={fmt(purchase.deleted_at)} "
                f"purchase_item=#{batch.purchase_item_id} qty={q(batch.quantity)} "
                f"cost={allocated_cost} participates={reason == 'active'} reason={reason}"
            )
        self.stdout.write("")

    def _print_purchase_groups(self, purchase_items, active_batches):
        self.stdout.write("E. BY PURCHASE ITEM")
        batches_by_purchase_item = defaultdict(list)
        for batch in active_batches:
            batches_by_purchase_item[batch.purchase_item_id].append(batch)

        for item in purchase_items:
            item_batches = batches_by_purchase_item.get(item.id, [])
            sold_qty = sum((batch.quantity or ZERO_QTY for batch in item_batches), ZERO_QTY)
            revenue = sum((batch.total_amount or ZERO_MONEY for batch in item_batches), ZERO_MONEY)
            remaining_qty = item.quantity_kg - sold_qty
            if remaining_qty < ZERO_QTY:
                remaining_qty = ZERO_QTY
            avg_sale_price = m(revenue / sold_qty) if sold_qty > ZERO_QTY else ZERO_MONEY
            sold_cost = m(sold_qty * item.purchase_price_per_kg)
            profit = m(revenue - sold_cost)
            sale_refs = ", ".join(
                f"sale#{batch.sale_item.sale_id}/item#{batch.sale_item_id}/{q(batch.quantity)}kg"
                for batch in item_batches
            ) or "-"
            self.stdout.write(
                "  "
                f"purchase=#{item.purchase_id} item=#{item.id} purchased={q(item.quantity_kg)} "
                f"sold_fifo={q(sold_qty)} remaining={q(remaining_qty)} "
                f"avg_sale={avg_sale_price} sold_cost={sold_cost} profit={profit} sales=[{sale_refs}]"
            )
        self.stdout.write("")

    def _print_supplier_payment_allocations(self, *, store, purchase_ids):
        self.stdout.write("F. SUPPLIER PAYMENT ALLOCATIONS")
        if not purchase_ids:
            self.stdout.write("  No selected purchases, skipping payment allocation diagnostics.")
            self.stdout.write("")
            return

        purchase_totals = {
            row["purchase_id"]: row["total"] or ZERO_MONEY
            for row in PurchaseItem.objects.filter(
                purchase_id__in=purchase_ids,
                store=store,
                purchase__deleted_at__isnull=True,
            )
            .values("purchase_id")
            .annotate(total=Sum(models_f("quantity_kg", "purchase_price_per_kg")))
        }

        allocated_totals = defaultdict(Decimal)
        allocations = (
            SupplierPaymentAllocation.objects.select_related("payment", "purchase", "store")
            .filter(purchase_id__in=purchase_ids, store=store)
            .order_by("purchase_id", "payment__date", "payment_id", "id")
        )
        for allocation in allocations:
            allocated_totals[allocation.purchase_id] += allocation.amount or ZERO_MONEY

        for purchase_id in purchase_ids:
            total = m(purchase_totals.get(purchase_id, ZERO_MONEY))
            allocated = m(allocated_totals.get(purchase_id, ZERO_MONEY))
            remaining = m(total - allocated)
            self.stdout.write(
                f"  purchase=#{purchase_id} purchase_total={total} allocated_payments={allocated} remaining_debt={remaining}"
            )
            purchase_allocations = [allocation for allocation in allocations if allocation.purchase_id == purchase_id]
            if purchase_allocations:
                for allocation in purchase_allocations:
                    payment = allocation.payment
                    deleted_at = getattr(payment, "deleted_at", None)
                    self.stdout.write(
                        "    "
                        f"allocation=#{allocation.id} payment=#{payment.id} payment_date={payment.date} "
                        f"payment_amount={m(payment.amount)} allocated={m(allocation.amount)} payment_deleted_at={fmt(deleted_at)}"
                    )
            else:
                self.stdout.write("    no payment allocations")
        self.stdout.write("")

    def _print_orphans(self):
        self.stdout.write("G. ORPHAN CHECKS")
        queries = [
            (
                "sale_items_without_sale",
                "SELECT COUNT(*) FROM sales_saleitem si "
                "LEFT JOIN sales_sale s ON s.id = si.sale_id WHERE s.id IS NULL",
            ),
            (
                "fifo_allocations_without_sale_item",
                "SELECT COUNT(*) FROM sale_item_batches sib "
                "LEFT JOIN sales_saleitem si ON si.id = sib.sale_item_id WHERE si.id IS NULL",
            ),
            (
                "fifo_allocations_without_purchase_item",
                "SELECT COUNT(*) FROM sale_item_batches sib "
                "LEFT JOIN inventory_purchaseitem pi ON pi.id = sib.purchase_item_id WHERE pi.id IS NULL",
            ),
            (
                "supplier_payment_allocations_without_purchase",
                "SELECT COUNT(*) FROM payables_supplierpaymentallocation spa "
                "LEFT JOIN inventory_purchase p ON p.id = spa.purchase_id WHERE p.id IS NULL",
            ),
            (
                "supplier_payment_allocations_without_payment",
                "SELECT COUNT(*) FROM payables_supplierpaymentallocation spa "
                "LEFT JOIN payables_supplierpayment sp ON sp.id = spa.payment_id WHERE sp.id IS NULL",
            ),
        ]
        with connection.cursor() as cursor:
            for label, sql in queries:
                cursor.execute(sql)
                self.stdout.write(f"  {label}: {cursor.fetchone()[0]}")


def models_f(quantity_field, price_field):
    from django.db.models import DecimalField, ExpressionWrapper, F

    return ExpressionWrapper(
        F(quantity_field) * F(price_field),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
