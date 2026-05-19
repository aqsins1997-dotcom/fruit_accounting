from django.core.management.base import BaseCommand

from apps.sales.forensics import (
    apply_saleitem_batch_mismatch_repair,
    build_saleitem_batch_mismatch_repair_plan,
)


class Command(BaseCommand):
    help = "Dry-run first, then safe targeted repair for the SaleItem #14-style batch mismatch."

    def add_arguments(self, parser):
        parser.add_argument("--sale-item-id", type=int, default=14)
        parser.add_argument("--purchase-item-id", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        sale_item_id = options["sale_item_id"]
        purchase_item_id = options.get("purchase_item_id")
        apply_changes = options.get("apply", False)

        plan = build_saleitem_batch_mismatch_repair_plan(
            sale_item_id=sale_item_id,
            purchase_item_id=purchase_item_id,
        )
        self.stdout.write("DRY RUN" if not apply_changes else "APPLY MODE")
        self._print_plan(plan)

        if not plan["repairable"]:
            self.stdout.write(f"UNSAFE: {plan['reason']}")
            return

        if not apply_changes:
            self.stdout.write("WOULD APPLY: safe transfer plan is available.")
            return

        result = apply_saleitem_batch_mismatch_repair(
            sale_item_id=sale_item_id,
            purchase_item_id=purchase_item_id,
        )
        self.stdout.write("")
        self.stdout.write("AFTER")
        self._print_plan(result["after"])
        self.stdout.write("")
        self.stdout.write(
            "audit_accounting_integrity summary: "
            f"CRITICAL={result['audit_summary']['critical']}, "
            f"WARNING={result['audit_summary']['warning']}, "
            f"INFO={result['audit_summary']['info']}"
        )

    def _print_plan(self, plan):
        self.stdout.write(f"SaleItem #{plan['sale_item_id']}")
        self.stdout.write(f"  sale id: {plan['sale_id']}")
        self.stdout.write(f"  date: {plan['sale_date']}")
        self.stdout.write(f"  customer: {plan['customer']}")
        self.stdout.write(f"  product: {plan['product']}")
        self.stdout.write(f"  store: {plan['store']}")
        self.stdout.write(f"  sale quantity: {plan['sale_quantity']}")
        self.stdout.write(f"  allocated quantity: {plan['allocated_quantity']}")
        self.stdout.write(f"  shortfall quantity: {plan['shortfall_quantity']}")
        self.stdout.write(f"  line total: {plan['line_total']}")
        self.stdout.write(f"  line cost total: {plan['line_cost_total']}")
        self.stdout.write(f"  profit: {plan['profit']}")
        self.stdout.write(f"  repairable: {plan['repairable']}")
        self.stdout.write(f"  reason: {plan['reason']}")
        self.stdout.write("  current target batches:")
        if not plan["target_batches"]:
            self.stdout.write("    - none")
        else:
            for batch in plan["target_batches"]:
                self.stdout.write(
                    "    - "
                    f"batch #{batch['batch_id']} purchase/allocation qty={batch['allocated_quantity']}"
                )
        self.stdout.write("  later candidate allocations on the same purchase batch:")
        if not plan["candidate_later_batches"]:
            self.stdout.write("    - none")
        else:
            for batch in plan["candidate_later_batches"]:
                self.stdout.write(
                    "    - "
                    f"batch #{batch['batch_id']}, sale_item #{batch['sale_item_id']}, "
                    f"sale #{batch['sale_id']}, date {batch['sale_date']}, allocated={batch['allocated_quantity']}"
                )
        if plan["selected_candidate"]:
            candidate = plan["selected_candidate"]
            alternative = plan["selected_alternative_purchase_item"]
            self.stdout.write("  selected safe transfer:")
            self.stdout.write(
                "    - "
                f"move {plan['shortfall_quantity']} from batch #{candidate['batch_id']} "
                f"(sale_item #{candidate['sale_item_id']}) to purchase_item #{alternative['purchase_item_id']} "
                f"(purchase #{alternative['purchase_id']}, date {alternative['purchase_date']}, "
                f"available={alternative['available_quantity']})"
            )
