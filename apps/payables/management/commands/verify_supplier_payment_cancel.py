from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.payables.models import SupplierPayment, SupplierPaymentAllocation
from apps.payables.services import calculate_supplier_remaining_debt
from apps.sales.models import CashRegister
from apps.sales.services import build_cash_breakdown


ZERO = Decimal("0.00")


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Read-only verification for supplier payment cancellation, cash impact, and allocation effect."

    def add_arguments(self, parser):
        parser.add_argument("--payment-id", type=int, required=True)

    def handle(self, *args, **options):
        payment_id = options["payment_id"]
        try:
            payment = (
                SupplierPayment.objects.select_related("supplier", "store", "purchase")
                .prefetch_related("allocations__purchase")
                .get(pk=payment_id)
            )
        except SupplierPayment.DoesNotExist as exc:
            raise CommandError(f"SupplierPayment #{payment_id} does not exist.") from exc

        allocation_total = _money(payment.allocations.aggregate(total=Sum("amount"))["total"])
        active_allocation_total = _money(
            SupplierPaymentAllocation.objects.filter(
                payment=payment,
                payment__status=SupplierPayment.STATUS_ACTIVE,
            ).aggregate(total=Sum("amount"))["total"]
        )
        ignored_cancelled_allocation_total = _money(
            allocation_total - active_allocation_total
            if payment.status == SupplierPayment.STATUS_CANCELLED
            else ZERO
        )

        cash_impact_while_active = ZERO
        cash_impact_now = ZERO
        expected_cancel_return = ZERO
        if payment.payment_method == SupplierPayment.PAYMENT_METHOD_CASH:
            cash_impact_while_active = -_money(payment.amount)
            if payment.status == SupplierPayment.STATUS_ACTIVE:
                cash_impact_now = cash_impact_while_active
                expected_cancel_return = _money(payment.amount)

        register = CashRegister.objects.filter(store=payment.store).first()
        stored_cash = _money(register.balance if register else ZERO)
        cash_breakdown = build_cash_breakdown(payment.store)

        current_debt = _money(
            calculate_supplier_remaining_debt(
                supplier_id=payment.supplier_id,
                store_id=payment.store_id,
            )
        )
        debt_without_payment = current_debt
        if payment.status == SupplierPayment.STATUS_ACTIVE:
            debt_without_payment = _money(
                calculate_supplier_remaining_debt(
                    supplier_id=payment.supplier_id,
                    store_id=payment.store_id,
                    exclude_payment_id=payment.id,
                )
            )

        self.stdout.write("READ ONLY: no data will be changed.")
        self.stdout.write(f"payment id: {payment.id}")
        self.stdout.write(f"supplier: {payment.supplier.name} (id={payment.supplier_id})")
        self.stdout.write(f"store: {payment.store.name} (id={payment.store_id})")
        self.stdout.write(f"purchase id: {payment.purchase_id or '-'}")
        self.stdout.write(f"amount: {_money(payment.amount)}")
        self.stdout.write(f"payment_method: {payment.payment_method}")
        self.stdout.write(f"status: {payment.status}")
        self.stdout.write(f"created_at: {payment.created_at}")
        self.stdout.write(f"cancelled_at: {payment.cancelled_at or '-'}")
        self.stdout.write("")
        self.stdout.write("allocations:")
        if payment.allocations.exists():
            for allocation in payment.allocations.all():
                self.stdout.write(
                    "  "
                    f"allocation id={allocation.id}, "
                    f"purchase id={allocation.purchase_id}, "
                    f"purchase date={allocation.purchase.date}, "
                    f"store id={allocation.store_id}, "
                    f"amount={_money(allocation.amount)}"
                )
        else:
            self.stdout.write("  none")
        self.stdout.write(f"allocation total: {allocation_total}")
        self.stdout.write(f"active allocation effect: {active_allocation_total}")
        self.stdout.write(f"cancelled allocation ignored effect: {ignored_cancelled_allocation_total}")
        self.stdout.write("")
        self.stdout.write("cash:")
        self.stdout.write(f"cash impact while active: {cash_impact_while_active}")
        self.stdout.write(f"cash impact now: {cash_impact_now}")
        self.stdout.write(f"expected cash return if cancelled now: {expected_cancel_return}")
        self.stdout.write(f"stored cash balance: {stored_cash}")
        self.stdout.write(f"formula cash balance: {cash_breakdown['formula_balance']}")
        self.stdout.write(f"stored minus formula: {cash_breakdown['difference']}")
        self.stdout.write("")
        self.stdout.write("supplier debt:")
        self.stdout.write(f"current supplier debt: {current_debt}")
        self.stdout.write(f"debt without this payment: {debt_without_payment}")
        self.stdout.write(f"current active effect of this payment: {_money(debt_without_payment - current_debt)}")
