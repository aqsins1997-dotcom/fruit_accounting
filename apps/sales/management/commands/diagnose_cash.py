from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Store
from apps.sales.services import build_cash_breakdown


class Command(BaseCommand):
    help = "Read-only cash register diagnostics by income/outflow source."

    def add_arguments(self, parser):
        parser.add_argument("--store", help="Optional store name or name fragment.")

    def handle(self, *args, **options):
        stores = self._stores(options.get("store"))
        if not stores:
            raise CommandError("No stores found.")

        self.stdout.write("READ ONLY DIAGNOSTIC: no data will be changed.")
        self.stdout.write("")

        total_formula = Decimal("0.00")
        total_stored = Decimal("0.00")
        for store in stores:
            row = build_cash_breakdown(store)
            total_formula += row["formula_balance"]
            total_stored += row["stored_balance"]
            self._print_store(row)

        if len(stores) > 1:
            self.stdout.write("ALL STORES")
            self.stdout.write(f"  stored cash register balance: {total_stored}")
            self.stdout.write(f"  formula current cash:         {total_formula}")
            self.stdout.write(f"  stored - formula difference:  {total_stored - total_formula}")

    def _stores(self, term):
        queryset = Store.objects.order_by("name")
        if not term:
            return list(queryset)

        stores = list(queryset.filter(name__icontains=term))
        if len(stores) > 1:
            self.stdout.write(f"Multiple stores matched {term!r}; using all matches:")
            for store in stores:
                self.stdout.write(f"  #{store.id} {store.name}")
            self.stdout.write("")
        return stores

    def _print_store(self, row):
        self.stdout.write(f"STORE #{row['store'].id}: {row['store'].name}")
        self.stdout.write("  INCOME INCLUDED IN CASH")
        self.stdout.write(
            f"    cash sales:                    {row['cash_sales']} "
            f"({row['cash_sales_count']} sales)"
        )
        self.stdout.write(
            f"    customer debt payments:        {row['client_debt_payments']} "
            f"({row['client_debt_payments_count']} payments)"
        )
        if row["client_debt_payments_by_method"]:
            for method, method_row in sorted(row["client_debt_payments_by_method"].items()):
                self.stdout.write(
                    f"      - {method}: {method_row['total']} ({method_row['count']} payments)"
                )
        self.stdout.write(f"    legacy credit payments:        {row['legacy_credit_payments']}")
        self.stdout.write("")
        self.stdout.write("  EXCLUDED FROM CASH")
        self.stdout.write(
            f"    credit sales:                  {row['credit_sales']} "
            f"({row['credit_sales_count']} sales)"
        )
        self.stdout.write("")
        self.stdout.write("  CASH OUTFLOWS")
        self.stdout.write(f"    supplier payments from cash:   {row['supplier_payments']}")
        self.stdout.write(f"    employee advances:             {row['employee_advances']}")
        self.stdout.write(f"    store expenses:                {row['store_expenses']}")
        self.stdout.write(f"    salary payments:               {row['salary_payments']}")
        self.stdout.write("")
        self.stdout.write("  FORMULA")
        self.stdout.write(
            "    current cash = cash_sales + customer_debt_payments + legacy_credit_payments "
            "- supplier_payments - employee_advances - store_expenses - salary_payments"
        )
        self.stdout.write(f"    current cash by formula:       {row['formula_balance']}")
        self.stdout.write(f"    stored cash register balance:  {row['stored_balance']}")
        self.stdout.write(f"    stored - formula difference:   {row['difference']}")

        if row["difference"] == row["credit_sales"] and row["credit_sales"] != Decimal("0.00"):
            self.stdout.write(
                "    WARNING: stored balance is higher than formula by exactly the credit sales total."
            )
        elif row["difference"] != Decimal("0.00"):
            self.stdout.write(
                "    NOTE: difference means manual/untracked balance change or older inconsistent cash writes."
            )
        self.stdout.write("")
