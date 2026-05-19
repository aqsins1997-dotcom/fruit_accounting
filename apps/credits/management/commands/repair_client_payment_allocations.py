from django.core.management.base import BaseCommand
from django.db import transaction

from apps.credits.services import repair_client_payment_allocations


class Command(BaseCommand):
    help = "Repairs active client debt payments that do not have credit allocations."

    def handle(self, *args, **options):
        with transaction.atomic():
            report = repair_client_payment_allocations()

        self.stdout.write(f"Found payments without allocations: {report['found_count']}")

        if report["clients"]:
            self.stdout.write("Clients:")
            for client in report["clients"]:
                self.stdout.write(
                    f"- {client['store_name']} / {client['client_name']} (store_id={client['store_id']}, client_id={client['client_id']})"
                )

        self.stdout.write(f"Total allocated: {report['total_allocated']}")

        if report["payments"]:
            self.stdout.write("Payments:")
            for payment in report["payments"]:
                self.stdout.write(
                    f"- payment_id={payment['payment_id']} client={payment['client_name']} store={payment['store_name']} "
                    f"amount={payment['payment_amount']} allocated={payment['allocated_amount']} leftover={payment['leftover_amount']}"
                )

        if report["unallocated"]:
            self.stdout.write(self.style.WARNING("Unallocated leftovers:"))
            for payment in report["unallocated"]:
                self.stdout.write(
                    f"- payment_id={payment['payment_id']} client={payment['client_name']} leftover={payment['leftover_amount']}"
                )
        else:
            self.stdout.write(self.style.SUCCESS("All found client payments were fully allocated."))
