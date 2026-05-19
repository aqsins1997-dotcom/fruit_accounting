from django.core.management.base import BaseCommand

from apps.sales.forensics import build_purchase_item_allocation_report


class Command(BaseCommand):
    help = "Read-only forensic report for one purchase batch and its linked sale allocations."

    def add_arguments(self, parser):
        parser.add_argument("--purchase-item-id", type=int, required=True)
        parser.add_argument("--batch-id", type=int)
        parser.add_argument("--reference-sale-item-id", type=int, default=14)

    def handle(self, *args, **options):
        report = build_purchase_item_allocation_report(
            purchase_item_id=options["purchase_item_id"],
            batch_id=options.get("batch_id"),
            reference_sale_item_id=options.get("reference_sale_item_id"),
        )

        self.stdout.write(f"purchase_item #{report['purchase_item_id']}")
        self.stdout.write(f"  purchase id: {report['purchase_id']}")
        self.stdout.write(f"  date: {report['purchase_date']}")
        self.stdout.write(f"  supplier: {report['supplier']}")
        self.stdout.write(f"  product: {report['product']}")
        self.stdout.write(f"  store: {report['store']}")
        self.stdout.write(f"  purchase quantity: {report['purchase_quantity']}")
        self.stdout.write(f"  purchase remaining_stock: {report['purchase_remaining_stock']}")
        self.stdout.write(f"  purchase price per kg: {report['purchase_price_per_kg']}")
        self.stdout.write(f"  purchase total cost: {report['purchase_total_cost']}")
        self.stdout.write("")
        self.stdout.write("linked batch allocations:")
        if not report["allocations"]:
            self.stdout.write("  - none")
        else:
            for row in report["allocations"]:
                self.stdout.write(
                    "  - "
                    f"batch #{row['batch_id']}, sale_item #{row['sale_item_id']}, sale #{row['sale_id']}, "
                    f"date {row['sale_date']}, customer={row['customer']}, product={row['product']}, store={row['store']}, "
                    f"sale_qty={row['sale_quantity']}, allocated={row['allocated_quantity']}, "
                    f"line_total={row['line_total']}, line_cost_total={row['line_cost_total']}, profit={row['profit']}, "
                    f"created_at={row['created_at']}"
                )
        self.stdout.write("")
        self.stdout.write(f"sum allocated: {report['sum_allocated']}")
        self.stdout.write(
            "check purchase_qty - sum_allocated - purchase_remaining_stock: "
            f"{report['equation_check']}"
        )
        self.stdout.write("")
        self.stdout.write(
            f"sales after SaleItem #{report['reference_sale_item_id']} on this purchase item:"
        )
        if not report["later_allocations"]:
            self.stdout.write("  - none")
        else:
            for row in report["later_allocations"]:
                self.stdout.write(
                    "  - "
                    f"batch #{row['batch_id']}, sale_item #{row['sale_item_id']}, sale #{row['sale_id']}, "
                    f"date {row['sale_date']}, allocated={row['allocated_quantity']}, created_at={row['created_at']}"
                )
        self.stdout.write(f"later allocations total: {report['later_allocations_total']}")
