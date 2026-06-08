from django.core.management.base import BaseCommand, CommandError

from apps.credits.models import Credit
from apps.credits.services import get_client_debt
from apps.sales.models import CashRegister, Sale
from apps.sales.services import build_cash_breakdown


def _money(value):
    return f"{value:.2f}"


class Command(BaseCommand):
    help = "Read-only verification of one sale payment type, cash effect, and client debt effect."

    def add_arguments(self, parser):
        parser.add_argument("--sale-id", type=int, required=True)

    def handle(self, *args, **options):
        sale_id = options["sale_id"]
        try:
            sale = Sale.objects.select_related("store", "customer").get(pk=sale_id)
        except Sale.DoesNotExist as exc:
            raise CommandError(f"Sale #{sale_id} does not exist.") from exc

        register = CashRegister.objects.filter(store=sale.store).first()
        cash_breakdown = build_cash_breakdown(sale.store)
        credit = Credit.objects.select_related("customer", "store").filter(sale=sale).first()

        cash_effect_expected = (
            sale.total_amount
            if sale.payment_type == Sale.PAYMENT_TYPE_CASH and not sale.deleted_at
            else 0
        )
        client_debt_effect_expected = (
            sale.total_amount
            if sale.payment_type == Sale.PAYMENT_TYPE_CREDIT and not sale.deleted_at
            else 0
        )
        current_client_debt = None
        if sale.customer_id:
            current_client_debt = get_client_debt(store=sale.store, client=sale.customer)

        if sale.payment_type == Sale.PAYMENT_TYPE_CREDIT:
            represented_correctly = bool(
                credit
                and credit.customer_id == sale.customer_id
                and credit.store_id == sale.store_id
                and credit.original_amount == sale.total_amount
            )
        else:
            represented_correctly = credit is None

        self.stdout.write("READ ONLY SALE PAYMENT TYPE VERIFICATION: no data will be changed.")
        self.stdout.write(f"sale id: {sale.id}")
        self.stdout.write(f"date: {sale.date}")
        self.stdout.write(f"store: {sale.store.name} (id={sale.store_id})")
        self.stdout.write(f"payment type: {sale.payment_type}")
        self.stdout.write(f"client: {sale.customer or '-'}")
        self.stdout.write(f"total: {_money(sale.total_amount)}")
        self.stdout.write(f"cash effect expected: {_money(cash_effect_expected)}")
        self.stdout.write(f"client debt effect expected: {_money(client_debt_effect_expected)}")
        self.stdout.write(f"current cash balance: {_money(register.balance if register else 0)}")
        self.stdout.write(f"formula cash balance: {_money(cash_breakdown['formula_balance'])}")
        self.stdout.write(f"stored minus formula: {_money(cash_breakdown['difference'])}")
        self.stdout.write(f"credit record id: {credit.id if credit else '-'}")
        if credit:
            self.stdout.write(f"credit original amount: {_money(credit.original_amount)}")
            self.stdout.write(f"credit remaining amount: {_money(credit.remaining_amount)}")
            self.stdout.write(f"credit status: {credit.status}")
        self.stdout.write(
            "current client debt: "
            + (_money(current_client_debt) if current_client_debt is not None else "-")
        )
        self.stdout.write(f"represented correctly in cash/client debt: {'YES' if represented_correctly else 'NO'}")
