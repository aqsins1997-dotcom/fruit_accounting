from django.core.management.base import BaseCommand

from apps.sales.services import recalculate_cash_registers


class Command(BaseCommand):
    help = "Recalculate cash register balances from sales, credit payments, supplier payments, and cash expenses."

    def handle(self, *args, **options):
        results = recalculate_cash_registers()
        if not results:
            self.stdout.write(self.style.WARNING("No stores found."))
            return

        for result in results:
            self.stdout.write(
                f"{result['store']}: {result['old_balance']} -> {result['new_balance']}"
            )

        self.stdout.write(self.style.SUCCESS("Cash registers recalculated."))
