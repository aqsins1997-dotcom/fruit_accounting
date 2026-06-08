from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Customer
from apps.sales.models import Sale
from apps.sales.services import convert_sale_cash_to_credit, preview_sale_cash_to_credit


def _format_money(value):
    return f"{value:.2f}"


def _validation_message(exc):
    if hasattr(exc, "message_dict"):
        return "; ".join(
            str(message)
            for messages in exc.message_dict.values()
            for message in messages
        )
    return "; ".join(str(message) for message in exc.messages)


class Command(BaseCommand):
    help = "Safely convert one cash sale to credit. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--sale-id", type=int, required=True)
        parser.add_argument("--client-id", type=int)
        parser.add_argument("--client-name")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        sale_id = options["sale_id"]
        customer = self._resolve_customer(
            client_id=options.get("client_id"),
            client_name=options.get("client_name"),
            apply=options["apply"],
        )
        try:
            preview = preview_sale_cash_to_credit(sale_id=sale_id, customer=customer)
        except Sale.DoesNotExist as exc:
            raise CommandError(f"Sale #{sale_id} does not exist.") from exc
        sale = preview["sale"]

        self.stdout.write("DRY RUN" if not options["apply"] else "APPLY")
        self.stdout.write(f"sale id: {sale.id}")
        self.stdout.write(f"date: {sale.date}")
        self.stdout.write(f"store: {sale.store.name} (id={sale.store_id})")
        self.stdout.write(f"current payment type: {sale.payment_type}")
        self.stdout.write(f"current client: {sale.customer or '-'}")
        self.stdout.write(f"new client: {customer or '-'}")
        self.stdout.write(f"sale total: {_format_money(sale.total_amount)}")
        self.stdout.write(f"cash impact: {_format_money(preview['cash_impact'])}")
        self.stdout.write(f"client debt impact: +{_format_money(preview['client_debt_impact'])}")
        self.stdout.write(f"inventory impact: {preview['inventory_impact']}")
        self.stdout.write(f"supplier debt impact: {preview['supplier_debt_impact']}")
        self.stdout.write(f"can_apply: {'YES' if preview['can_apply'] else 'NO'}")
        if preview["error"]:
            self.stdout.write(f"error: {preview['error']}")

        if not options["apply"]:
            return
        if not preview["can_apply"]:
            raise CommandError(preview["error"] or "Cannot apply conversion.")

        try:
            converted_sale = convert_sale_cash_to_credit(
                sale_id=sale.id,
                customer_id=customer.id,
                note=f"Converted from cash to credit by management command. Client: {customer}.",
            )
        except ValidationError as exc:
            raise CommandError(_validation_message(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"converted sale #{converted_sale.id} to credit"))

    def _resolve_customer(self, *, client_id, client_name, apply):
        if client_id and client_name:
            raise CommandError("Use either --client-id or --client-name, not both.")
        if client_id:
            try:
                return Customer.objects.get(pk=client_id)
            except Customer.DoesNotExist as exc:
                raise CommandError(f"Customer #{client_id} does not exist.") from exc
        if client_name:
            matches = list(Customer.objects.filter(name__iexact=client_name).order_by("id")[:2])
            if not matches:
                raise CommandError(f'Customer "{client_name}" was not found.')
            if len(matches) > 1:
                raise CommandError(f'Customer name "{client_name}" is ambiguous. Use --client-id.')
            return matches[0]
        if apply:
            raise CommandError("Use --client-id or --client-name with --apply.")
        return None
