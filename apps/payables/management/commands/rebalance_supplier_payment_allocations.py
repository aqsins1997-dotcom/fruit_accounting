from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.inventory.models import PurchaseItem
from apps.payables.services import apply_purchase_item_rebalance_update, build_purchase_item_rebalance_preview


class Command(BaseCommand):
    help = "Dry-run and apply supplier payment allocation rebalance for a purchase item quantity or price change."

    def add_arguments(self, parser):
        parser.add_argument("--purchase-item-id", type=int, required=True)
        parser.add_argument("--new-quantity", type=Decimal)
        parser.add_argument("--new-unit-price", type=Decimal)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        purchase_item_id = options["purchase_item_id"]
        new_quantity = options.get("new_quantity")
        new_unit_price = options.get("new_unit_price")
        apply_changes = options.get("apply", False)

        if (new_quantity is None and new_unit_price is None) or (
            new_quantity is not None and new_unit_price is not None
        ):
            raise CommandError("Specify exactly one of --new-quantity or --new-unit-price.")

        purchase_item = PurchaseItem.objects.select_related(
            "purchase__supplier",
            "store",
            "product",
        ).get(pk=purchase_item_id, purchase__deleted_at__isnull=True)

        preview = build_purchase_item_rebalance_preview(
            purchase_item=purchase_item,
            new_quantity=new_quantity,
            new_unit_price=new_unit_price,
        )
        self.stdout.write("DRY RUN" if not apply_changes else "APPLY MODE")
        self._print_preview(preview)

        if not apply_changes:
            return

        result = apply_purchase_item_rebalance_update(
            purchase_item=purchase_item,
            new_quantity=new_quantity,
            new_unit_price=new_unit_price,
        )
        self.stdout.write("")
        self.stdout.write("AFTER APPLY")
        self._print_preview(result["after"])

    def _print_preview(self, preview):
        self.stdout.write(f"purchase item: {preview['purchase_item_id']}")
        self.stdout.write(f"supplier: {preview['supplier_name']}")
        self.stdout.write(f"store: {preview['store_name']}")
        self.stdout.write(f"product: {preview['product_name']}")
        self.stdout.write(f"old quantity: {preview['old_quantity']}")
        self.stdout.write(f"new quantity: {preview['new_quantity']}")
        self.stdout.write(f"old unit price: {preview['old_unit_price']}")
        self.stdout.write(f"new unit price: {preview['new_unit_price']}")
        self.stdout.write(f"sold quantity: {preview['sold_quantity']}")
        self.stdout.write(f"remaining stock: {preview['remaining_stock']}")
        self.stdout.write(f"old purchase amount: {preview['old_purchase_amount']}")
        self.stdout.write(f"new purchase amount: {preview['new_purchase_amount']}")
        self.stdout.write(f"old allocated payment: {preview['old_allocated_payment']}")
        self.stdout.write(f"new allocated payment for this purchase: {preview['new_allocated_payment']}")
        self.stdout.write(f"excess payment: {preview['excess_payment']}")
        self.stdout.write(f"cash changes: {preview['cash_change']}")
        if preview["redistributions"]:
            self.stdout.write("redistribution targets:")
            for row in preview["redistributions"]:
                self.stdout.write(
                    f"- purchase #{row['purchase_id']} {row['purchase_date']}: {row['applied_amount']} "
                    f"(remaining after={row['remaining_amount_after']})"
                )
        if preview["overpayment_created"] > Decimal("0.00"):
            self.stdout.write(f"overpayment created: {preview['overpayment_created']}")
