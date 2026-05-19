from django.core.management.base import BaseCommand

from apps.sales.allocation_repair import apply_saleitem_allocation_repair, iter_saleitem_allocation_mismatches


class Command(BaseCommand):
    help = "Dry-run forensic inspection and safe repair for corrupted SaleItem batch allocations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sale-item-id",
            type=int,
            help="Inspect or repair only one SaleItem.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply safe one-batch repairs. Without this flag the command is dry-run only.",
        )

    def handle(self, *args, **options):
        sale_item_id = options.get("sale_item_id")
        apply_changes = options.get("apply", False)

        reports = list(iter_saleitem_allocation_mismatches(sale_item_id=sale_item_id))
        self.stdout.write(
            "DRY RUN" if not apply_changes else "APPLY MODE"
        )
        self.stdout.write(f"Found mismatches: {len(reports)}")

        repaired = 0
        skipped = 0
        for report in reports:
            self._print_report(report)
            if not report["repairable"]:
                skipped += 1
                self.stdout.write(f"  SKIP: {report['reason']}")
                self.stdout.write("")
                continue

            if not apply_changes:
                self.stdout.write(
                    f"  WOULD REPAIR: set the only active batch to {report['proposed_quantity']} "
                    f"for purchase_item #{report['target_purchase_item_id']}."
                )
                self.stdout.write("")
                continue

            verified = apply_saleitem_allocation_repair(sale_item_id=report["sale_item_id"])
            repaired += 1
            self.stdout.write(
                f"  REPAIRED: allocated quantity is now {verified['allocated_quantity']} "
                f"for sale quantity {verified['sale_quantity']}."
            )
            self.stdout.write("")

        self.stdout.write(f"Repairable repaired: {repaired}")
        self.stdout.write(f"Skipped: {skipped}")

    def _print_report(self, report):
        self.stdout.write(f"SaleItem #{report['sale_item_id']}")
        self.stdout.write(f"  sale id: {report['sale_id']}")
        self.stdout.write(f"  date: {report['sale_date']}")
        self.stdout.write(f"  customer: {report['customer']}")
        self.stdout.write(f"  product: {report['product']}")
        self.stdout.write(f"  store: {report['store']}")
        self.stdout.write(f"  sale quantity: {report['sale_quantity']}")
        self.stdout.write(f"  allocated quantity: {report['allocated_quantity']}")
        self.stdout.write(f"  line total: {report['line_total']}")
        self.stdout.write(f"  line cost total: {report['line_cost_total']}")
        self.stdout.write(f"  profit: {report['profit']}")
        self.stdout.write("  linked batch allocations:")
        if not report["batches"]:
            self.stdout.write("    - none")
            return
        for batch in report["batches"]:
            self.stdout.write(
                "    - "
                f"batch #{batch['batch_id']}, purchase_item #{batch['purchase_item_id']}, "
                f"purchase #{batch['purchase_id']} ({batch['purchase_date']}), "
                f"allocated={batch['allocated_quantity']}, "
                f"purchase_qty={batch['purchase_quantity']}, "
                f"purchase_remaining_stock={batch['purchase_remaining_stock']}"
            )
