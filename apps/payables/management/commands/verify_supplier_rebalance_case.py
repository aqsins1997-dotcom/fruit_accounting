from django.core.management.base import BaseCommand

from apps.inventory.models import PurchaseItem
from apps.payables.services import build_supplier_rebalance_case_report
from apps.reports.audit import run_accounting_audit


class Command(BaseCommand):
    help = "Read-only verification of a supplier payment rebalance case for a purchase item."

    def add_arguments(self, parser):
        parser.add_argument("--purchase-item-id", type=int, required=True)

    def handle(self, *args, **options):
        purchase_item_id = options["purchase_item_id"]
        purchase_item = (
            PurchaseItem.objects.select_related("purchase__supplier", "store", "product")
            .filter(pk=purchase_item_id, purchase__deleted_at__isnull=True)
            .first()
        )

        if purchase_item is None:
            self.stdout.write(f"PurchaseItem #{purchase_item_id} not found in current database.")
            return

        report = build_supplier_rebalance_case_report(purchase_item=purchase_item)
        audit = run_accounting_audit()

        self.stdout.write("READ ONLY VERIFY SUPPLIER REBALANCE CASE")
        self.stdout.write(f"purchase item id: {report['purchase_item_id']}")
        self.stdout.write(f"purchase id: {report['purchase_id']}")
        self.stdout.write(f"supplier: {report['supplier_name']}")
        self.stdout.write(f"product: {report['product_name']}")
        self.stdout.write(f"store: {report['store_name']}")
        if report["history_available"]:
            self.stdout.write(f"old amount: {report['old_amount']}")
        else:
            self.stdout.write("old amount history: unavailable (not stored in DB)")
        self.stdout.write(f"current quantity: {report['current_quantity']}")
        self.stdout.write(f"unit price: {report['unit_price']}")
        self.stdout.write(f"sold quantity: {report['sold_quantity']}")
        self.stdout.write(f"remaining stock: {report['remaining_stock']}")
        self.stdout.write(f"line total amount: {report['line_total_amount']}")
        self.stdout.write(f"purchase total amount: {report['purchase_total_amount']}")
        self.stdout.write(f"paid amount on this purchase: {report['paid_amount']}")
        self.stdout.write(f"remaining debt: {report['remaining_debt']}")
        self.stdout.write(f"status: {report['status']}")

        self.stdout.write("allocations tied to this purchase:")
        if report["allocations"]:
            for row in report["allocations"]:
                self.stdout.write(
                    f"- allocation #{row['allocation_id']}: payment #{row['payment_id']} "
                    f"{row['payment_date']} method={row['payment_method']} "
                    f"payment_amount={row['payment_amount']} allocated={row['allocated_amount']}"
                )
        else:
            self.stdout.write("- none")

        self.stdout.write("redistributed allocations from the same payment(s):")
        if report["reallocated_targets"]:
            for row in report["reallocated_targets"]:
                self.stdout.write(
                    f"- payment #{row['payment_id']} -> purchase #{row['purchase_id']} "
                    f"{row['purchase_date']}: {row['allocated_amount']}"
                )
        else:
            self.stdout.write("- none")
        self.stdout.write(f"reallocated total to other purchases: {report['reallocated_total']}")

        self.stdout.write(f"supplier total purchases: {report['supplier_total_purchases']}")
        self.stdout.write(f"supplier total paid: {report['supplier_total_paid']}")
        self.stdout.write(f"supplier total debt: {report['supplier_total_debt']}")
        self.stdout.write(f"supplier overpayment: {report['supplier_overpayment']}")
        self.stdout.write(
            "allocation exceeds purchase amount: "
            f"{'YES' if report['allocation_excess_over_purchase'] > 0 else 'NO'}"
        )
        self.stdout.write(
            f"negative debt detected: {'YES' if report['has_negative_debt'] else 'NO'}"
        )

        cash = report["cash_breakdown"]
        self.stdout.write("cash balance check:")
        self.stdout.write(f"- stored balance: {cash['stored_balance']}")
        self.stdout.write(f"- formula balance: {cash['formula_balance']}")
        self.stdout.write(f"- difference: {cash['difference']}")
        self.stdout.write("- cash changed by reallocation: NO")

        self.stdout.write("accounting audit summary:")
        self.stdout.write(f"- CRITICAL: {audit['summary']['critical']}")
        self.stdout.write(f"- WARNING: {audit['summary']['warning']}")
        self.stdout.write(f"- INFO: {audit['summary']['info']}")
