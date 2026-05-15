from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import Store
from apps.sales.models import CashRegister
from apps.sales.services import build_cash_breakdown


class Command(BaseCommand):
    help = "Safely reconcile one cash register balance to the operation-based cash formula."

    def add_arguments(self, parser):
        parser.add_argument("--store", required=True, help="Store name or name fragment.")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true", help="Show the correction without changing data.")
        mode.add_argument("--apply", action="store_true", help="Apply the correction to CashRegister.balance.")

    def handle(self, *args, **options):
        store = self._find_store(options["store"])
        if options["dry_run"]:
            row = build_cash_breakdown(store)
            self._print_breakdown(row, dry_run=True)
            self.stdout.write("DRY RUN: no data was changed.")
            return

        with transaction.atomic():
            register, _ = CashRegister.objects.select_for_update().get_or_create(
                store=store,
                defaults={"balance": Decimal("0.00")},
            )
            row = build_cash_breakdown(store)
            old_balance = row["stored_balance"]
            new_balance = row["formula_balance"]
            difference = row["difference"]

            self._print_breakdown(row, dry_run=False)
            if old_balance == new_balance:
                self.stdout.write("No correction needed.")
                return

            register.balance = new_balance
            register.save(update_fields=["balance", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                "Applied cash correction: "
                f"store={store.name}, old_balance={old_balance}, "
                f"new_balance={new_balance}, difference={difference}"
            )
        )
        self.stdout.write("Comment: Коррекция после пересчёта кассы по операциям")

    def _find_store(self, term):
        matches = list(Store.objects.filter(name__icontains=term).order_by("name"))
        if not matches:
            raise CommandError(f"No store found for: {term}")
        if len(matches) > 1:
            exact_matches = [store for store in matches if store.name.lower() == term.lower()]
            if len(exact_matches) == 1:
                return exact_matches[0]

            matched = ", ".join(f"#{store.id} {store.name}" for store in matches)
            raise CommandError(f"Store name is ambiguous for {term!r}: {matched}")
        return matches[0]

    def _print_breakdown(self, row, *, dry_run):
        self.stdout.write(f"STORE #{row['store'].id}: {row['store'].name}")
        self.stdout.write(f"  current stored balance:      {row['stored_balance']}")
        self.stdout.write(f"  calculated balance:          {row['formula_balance']}")
        self.stdout.write(f"  difference:                  {row['difference']}")
        self.stdout.write("")
        self.stdout.write("  Formula:")
        self.stdout.write(f"    cash sales:                +{row['cash_sales']}")
        self.stdout.write(f"    customer debt payments:    +{row['client_debt_payments']}")
        self.stdout.write(f"    legacy credit payments:    +{row['legacy_credit_payments']}")
        self.stdout.write(f"    supplier payments:         -{row['supplier_payments']}")
        self.stdout.write(f"    employee advances:         -{row['employee_advances']}")
        self.stdout.write(f"    store expenses:            -{row['store_expenses']}")
        self.stdout.write(f"    salary payments:           -{row['salary_payments']}")
        self.stdout.write("")
        self.stdout.write(f"  Credit sales excluded:       {row['credit_sales']}")
        self.stdout.write(f"  Mode:                        {'dry-run' if dry_run else 'apply'}")
