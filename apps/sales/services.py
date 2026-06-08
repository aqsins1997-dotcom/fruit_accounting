from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models as django_models, transaction
from django.db.models import Count, DecimalField, Sum, Value
from django.db.models import Prefetch
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.models import Customer, Store
from apps.inventory.models import (
    PurchaseItem,
    StockMovement,
    StoreStock,
    sync_store_stock_from_active_inventory,
)

from .models import (
    CashRegister,
    Sale,
    SaleItem,
    SaleItemBatch,
    _apply_batch_costs,
    _apply_cash_register_delta,
    _sale_line_total,
    purchase_item_available_quantity,
)

ZERO = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _sum_amount(queryset, field_name="amount"):
    return queryset.aggregate(
        total=Coalesce(Sum(field_name), Value(ZERO, output_field=MONEY_FIELD))
    )["total"] or ZERO


def _validation_message(exc):
    if hasattr(exc, "message_dict"):
        return "; ".join(
            str(message)
            for messages in exc.message_dict.values()
            for message in messages
        )
    return "; ".join(str(message) for message in exc.messages)


def _append_sale_system_note(sale, note):
    if not note:
        return
    sale.comment = f"{sale.comment}\n{note}".strip() if sale.comment else note


def validate_sale_cash_to_credit(*, sale, customer):
    if not customer:
        raise ValidationError({"customer": "Выберите клиента для продажи в кредит."})
    if sale.deleted_at:
        raise ValidationError({"sale": "Удалённую продажу нельзя перевести в кредит."})
    if sale.payment_type != Sale.PAYMENT_TYPE_CASH:
        raise ValidationError({"payment_type": "Эта продажа уже не является наличной."})
    if sale.customer_id and sale.customer_id != customer.id:
        raise ValidationError({"customer": "У наличной продажи уже указан другой клиент."})


def preview_sale_cash_to_credit(*, sale_id, customer=None):
    sale = (
        Sale.objects.select_related("store", "customer")
        .prefetch_related("items__product", "items__batches__purchase_item__purchase__supplier")
        .get(pk=sale_id)
    )

    can_apply = True
    error = ""
    try:
        validate_sale_cash_to_credit(sale=sale, customer=customer)
    except ValidationError as exc:
        can_apply = False
        error = _validation_message(exc)

    return {
        "sale": sale,
        "customer": customer,
        "can_apply": can_apply,
        "error": error,
        "cash_impact": -_money(sale.total_amount),
        "client_debt_impact": _money(sale.total_amount) if customer else ZERO,
        "inventory_impact": "NO",
        "supplier_debt_impact": "NO",
    }


@transaction.atomic
def convert_sale_cash_to_credit(*, sale_id, customer_id, note=""):
    sale = Sale.objects.select_for_update().get(pk=sale_id)
    customer = Customer.objects.select_for_update().get(pk=customer_id)

    validate_sale_cash_to_credit(sale=sale, customer=customer)

    sale.payment_type = Sale.PAYMENT_TYPE_CREDIT
    sale.customer = customer
    _append_sale_system_note(sale, note)
    sale.save()
    sale.refresh_from_db()
    return sale


def build_cash_breakdown(store):
    from apps.credits.models import ClientDebtPayment, CreditPayment
    from apps.expenses.models import EmployeeAdvance, SalaryPayment, StoreExpense
    from apps.payables.models import SupplierPayment

    cash_sales = _sum_amount(
        Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CASH,
            deleted_at__isnull=True,
        ),
        "total_amount",
    )

    credit_sales = _sum_amount(
        Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            deleted_at__isnull=True,
        ),
        "total_amount",
    )

    client_debt_payment_queryset = ClientDebtPayment.objects.filter(
        store=store,
        status=ClientDebtPayment.STATUS_ACTIVE,
        payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
    )
    client_debt_payments = _sum_amount(client_debt_payment_queryset)
    client_debt_payments_by_method = {
        row["payment_method"]: {
            "count": row["count"],
            "total": _money(row["total"]),
        }
        for row in client_debt_payment_queryset.values("payment_method").annotate(
            count=Count("id"),
            total=Coalesce(Sum("amount"), Value(ZERO, output_field=MONEY_FIELD)),
        )
    }

    legacy_credit_payments = _sum_amount(
        CreditPayment.objects.filter(
            credit__store=store,
            credit__sale__deleted_at__isnull=True,
            client_debt_payment__isnull=True,
        )
    )
    supplier_payments = _sum_amount(
        SupplierPayment.objects.filter(
            store=store,
            status=SupplierPayment.STATUS_ACTIVE,
            payment_method=SupplierPayment.PAYMENT_METHOD_CASH,
        )
    )
    employee_advances = _sum_amount(EmployeeAdvance.objects.filter(store=store))
    store_expenses = _sum_amount(StoreExpense.objects.filter(store=store, deleted_at__isnull=True))
    salary_payments = _sum_amount(SalaryPayment.objects.filter(store=store))

    formula_balance = _money(
        cash_sales
        + client_debt_payments
        + legacy_credit_payments
        - supplier_payments
        - employee_advances
        - store_expenses
        - salary_payments
    )
    register = CashRegister.objects.filter(store=store).first()
    stored_balance = _money(register.balance if register else ZERO)

    return {
        "store": store,
        "stored_balance": stored_balance,
        "formula_balance": formula_balance,
        "difference": _money(stored_balance - formula_balance),
        "cash_sales": _money(cash_sales),
        "cash_sales_count": Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CASH,
            deleted_at__isnull=True,
        ).count(),
        "credit_sales": _money(credit_sales),
        "credit_sales_count": Sale.objects.filter(
            store=store,
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            deleted_at__isnull=True,
        ).count(),
        "client_debt_payments": _money(client_debt_payments),
        "client_debt_payments_count": client_debt_payment_queryset.count(),
        "client_debt_payments_by_method": client_debt_payments_by_method,
        "legacy_credit_payments": _money(legacy_credit_payments),
        "supplier_payments": _money(supplier_payments),
        "employee_advances": _money(employee_advances),
        "store_expenses": _money(store_expenses),
        "salary_payments": _money(salary_payments),
    }


def calculate_cash_balance(store):
    return build_cash_breakdown(store)["formula_balance"]


def _save_model_without_overrides(instance, **kwargs):
    django_models.Model.save(instance, **kwargs)


def _validate_selected_sale_item(*, sale_item, sale, purchase_item):
    if sale.deleted_at:
        raise ValidationError({"sale": "Нельзя изменять строки удаленной продажи."})
    if sale_item.quantity_kg is None or sale_item.quantity_kg <= Decimal("0.000"):
        raise ValidationError({"quantity_kg": "Количество должно быть больше 0."})
    if sale_item.sale_price_per_kg is None or sale_item.sale_price_per_kg < Decimal("0.00"):
        raise ValidationError({"sale_price_per_kg": "Цена продажи не может быть отрицательной."})
    if purchase_item.purchase.deleted_at:
        raise ValidationError({"purchase_item": "Нельзя списывать товар с удаленной закупки."})
    if purchase_item.store_id != sale.store_id:
        raise ValidationError({"purchase_item": "Выбранная закупка относится к другому магазину."})
    if purchase_item.product_id != sale_item.product_id:
        raise ValidationError({"purchase_item": "Выбранная закупка относится к другому товару."})

    available_quantity = purchase_item_available_quantity(purchase_item)
    if sale_item.quantity_kg > available_quantity:
        raise ValidationError(
            {
                "quantity_kg": (
                    "Недостаточно остатка в выбранной закупке. "
                    f"Доступно: {available_quantity} кг."
                )
            }
        )


def _sync_stock_after_fast_sale(*, sale, sale_item):
    stock, created = StoreStock.objects.select_for_update().get_or_create(
        store_id=sale.store_id,
        product_id=sale_item.product_id,
        defaults={
            "quantity_kg": Decimal("0.000"),
            "average_purchase_price": Decimal("0.00"),
        },
    )
    if created:
        sync_store_stock_from_active_inventory(store_id=sale.store_id, product_id=sale_item.product_id)
        return

    stock.quantity_kg -= sale_item.quantity_kg
    if stock.quantity_kg < Decimal("0.000"):
        stock.quantity_kg = Decimal("0.000")
    stock.save(update_fields=["quantity_kg", "updated_at"])


def _sync_sale_totals_after_fast_sale(*, sale, sale_item):
    sale.total_amount = sale_item.line_total
    sale.total_cost = sale_item.line_cost_total
    sale.total_profit = sale_item.profit
    now = timezone.now()
    Sale.objects.filter(pk=sale.pk).update(
        total_amount=sale.total_amount,
        total_cost=sale.total_cost,
        total_profit=sale.total_profit,
        updated_at=now,
    )

    if sale.payment_type == Sale.PAYMENT_TYPE_CASH:
        _apply_cash_register_delta(store_id=sale.store_id, amount=sale.total_amount)
        return

    if sale.payment_type == Sale.PAYMENT_TYPE_CREDIT:
        from apps.credits.models import Credit

        Credit.objects.create(
            sale=sale,
            customer=sale.customer,
            store=sale.store,
            original_amount=sale.total_amount,
            remaining_amount=sale.total_amount,
            status=Credit.STATUS_PAID if sale.total_amount == Decimal("0.00") else Credit.STATUS_UNPAID,
            comment=sale.comment,
        )


def _create_sale_item_from_selected_batch(sale_item):
    selected_purchase_item = getattr(sale_item, "_selected_purchase_item", None)
    selected_purchase_item_id = getattr(sale_item, "_selected_purchase_item_id", None)
    if selected_purchase_item is not None:
        selected_purchase_item_id = selected_purchase_item.pk

    if not selected_purchase_item_id:
        raise ValidationError({"purchase_item": "Выберите закупку/партию, с которой списывается товар."})

    sale = getattr(sale_item, "sale", None)
    if sale is None or not sale.pk:
        sale = Sale.objects.select_for_update().select_related("store").get(pk=sale_item.sale_id)
    purchase_item = (
        PurchaseItem.objects.select_for_update()
        .select_related("purchase", "purchase__supplier")
        .get(pk=selected_purchase_item_id)
    )

    _validate_selected_sale_item(sale_item=sale_item, sale=sale, purchase_item=purchase_item)

    sale_item.sale = sale
    sale_item.line_total = _sale_line_total(sale_item)
    total_cost = sale_item.quantity_kg * purchase_item.purchase_price_per_kg
    _apply_batch_costs(sale_item, total_cost)

    _save_model_without_overrides(sale_item, force_insert=True)

    now = timezone.now()
    SaleItemBatch.objects.bulk_create(
        [
            SaleItemBatch(
                created_at=now,
                updated_at=now,
                sale_item=sale_item,
                purchase_item=purchase_item,
                quantity=sale_item.quantity_kg,
                sale_price=sale_item.sale_price_per_kg,
                total_amount=sale_item.line_total,
            )
        ]
    )

    StockMovement.objects.create(
        store_id=sale.store_id,
        product_id=sale_item.product_id,
        movement_type="sale_out",
        quantity_kg_delta=sale_item.quantity_kg,
        unit_cost=sale_item.cost_price_per_kg,
        total_cost=sale_item.line_cost_total,
        reference_note=f"Sale #{sale.id}",
        date=sale.date,
    )
    _sync_stock_after_fast_sale(sale=sale, sale_item=sale_item)
    _sync_sale_totals_after_fast_sale(sale=sale, sale_item=sale_item)

    return sale_item


@transaction.atomic
def create_sale_from_valid_forms(*, sale_form, item_form):
    sale = sale_form.save(commit=False)
    sale.clean()
    _save_model_without_overrides(sale, force_insert=True)

    sale_item = item_form.save(commit=False)
    sale_item.sale = sale
    _create_sale_item_from_selected_batch(sale_item)
    return sale


@transaction.atomic
def recalculate_sale_costs_for_purchase_item(purchase_item):
    sale_item_ids = list(
        SaleItemBatch.objects.filter(
            purchase_item=purchase_item,
            sale_item__sale__deleted_at__isnull=True,
        )
        .values_list("sale_item_id", flat=True)
        .distinct()
    )
    if not sale_item_ids:
        return []

    now = timezone.now()
    changed_sale_ids = set()
    changed_sale_item_ids = []

    sale_items = (
        SaleItem.objects.select_for_update()
        .filter(id__in=sale_item_ids, sale__deleted_at__isnull=True)
        .prefetch_related(
            Prefetch(
                "batches",
                queryset=SaleItemBatch.objects.select_related("purchase_item").filter(
                    purchase_item__purchase__deleted_at__isnull=True,
                    sale_item__sale__deleted_at__isnull=True,
                ),
            )
        )
    )
    for sale_item in sale_items:
        total_cost = sum(
            (
                batch.quantity * batch.purchase_item.purchase_price_per_kg
                for batch in sale_item.batches.all()
            ),
            ZERO,
        )
        line_cost_total = _money(total_cost)
        if sale_item.quantity_kg > Decimal("0.000"):
            cost_price_per_kg = _money(total_cost / sale_item.quantity_kg)
        else:
            cost_price_per_kg = ZERO
        profit = _money(sale_item.line_total - line_cost_total)

        SaleItem.objects.filter(pk=sale_item.pk).update(
            cost_price_per_kg=cost_price_per_kg,
            line_cost_total=line_cost_total,
            profit=profit,
            updated_at=now,
        )
        changed_sale_ids.add(sale_item.sale_id)
        changed_sale_item_ids.append(sale_item.id)

    for sale in Sale.objects.select_for_update().filter(
        id__in=changed_sale_ids,
        deleted_at__isnull=True,
    ):
        totals = SaleItem.objects.filter(sale=sale).aggregate(
            total_cost=Coalesce(Sum("line_cost_total"), Value(ZERO, output_field=MONEY_FIELD)),
            total_profit=Coalesce(Sum("profit"), Value(ZERO, output_field=MONEY_FIELD)),
        )
        Sale.objects.filter(pk=sale.pk).update(
            total_cost=_money(totals["total_cost"]),
            total_profit=_money(totals["total_profit"]),
            updated_at=now,
        )

    return changed_sale_item_ids


@transaction.atomic
def recalculate_cash_registers(*, store=None):
    stores = Store.objects.filter(pk=store.pk) if store else Store.objects.all()
    results = []

    for target_store in stores.order_by("name"):
        register, _ = CashRegister.objects.select_for_update().get_or_create(
            store=target_store,
            defaults={"balance": ZERO},
        )
        old_balance = register.balance
        new_balance = calculate_cash_balance(target_store)
        register.balance = new_balance
        register.save(update_fields=["balance", "updated_at"])
        results.append(
            {
                "store": target_store,
                "old_balance": old_balance,
                "new_balance": new_balance,
            }
        )

    return results
