from django.core.management.base import BaseCommand
from django.db import transaction

from apps.payables.services import repair_supplier_payment_allocations


class Command(BaseCommand):
    help = "Repairs active supplier payments that do not have allocation rows."

    def handle(self, *args, **options):
        with transaction.atomic():
            report = repair_supplier_payment_allocations()

        self.stdout.write(f"Found payments without allocations: {report['found_count']}")

        if report["suppliers"]:
            self.stdout.write("Suppliers:")
            for supplier in report["suppliers"]:
                self.stdout.write(
                    f"- {supplier['store_name']} / {supplier['supplier_name']} (store_id={supplier['store_id']}, supplier_id={supplier['supplier_id']})"
                )

        self.stdout.write(f"Total allocated: {report['total_allocated']}")

        if report["payments"]:
            self.stdout.write("Payments:")
            for payment in report["payments"]:
                self.stdout.write(
                    f"- payment_id={payment['payment_id']} supplier={payment['supplier_name']} store={payment['store_name']} "
                    f"amount={payment['payment_amount']} allocated={payment['allocated_amount']} leftover={payment['leftover_amount']}"
                )

        if report["unallocated"]:
            self.stdout.write(self.style.WARNING("Unallocated leftovers:"))
            for payment in report["unallocated"]:
                self.stdout.write(
                    f"- payment_id={payment['payment_id']} supplier={payment['supplier_name']} leftover={payment['leftover_amount']}"
                )
        else:
            self.stdout.write(self.style.SUCCESS("All found supplier payments were fully allocated."))
