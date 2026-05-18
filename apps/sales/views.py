from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render

from .forms import SaleCreateForm, SaleItemCreateForm, purchase_item_options_data
from .models import CashRegister, Sale


def _attach_validation_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field_name, messages_list in exc.message_dict.items():
            target_field = field_name if field_name in form.fields else None
            for message in messages_list:
                form.add_error(target_field, message)
        return

    for message in exc.messages:
        form.add_error(None, message)


@login_required
def sale_create(request):
    if request.method == "POST":
        sale_form = SaleCreateForm(request.POST)
        item_form = SaleItemCreateForm(
            request.POST,
            store_id=request.POST.get("store"),
            product_id=request.POST.get("product"),
        )
        if sale_form.is_valid() and item_form.is_valid():
            try:
                with transaction.atomic():
                    sale = sale_form.save()
                    item = item_form.save(commit=False)
                    item.sale = sale
                    item.save()
            except ValidationError as exc:
                _attach_validation_error(item_form, exc)
            else:
                messages.success(request, "Продажа сохранена.")
                return redirect("sales:sale_list")
    else:
        sale_form = SaleCreateForm()
        item_form = SaleItemCreateForm()

    context = {
        "sale_form": sale_form,
        "item_form": item_form,
        "purchase_item_options": purchase_item_options_data(),
        "selected_purchase_item_id": request.POST.get("purchase_item", "") if request.method == "POST" else "",
    }
    return render(request, "sales/sale_form.html", context)


@login_required
def sale_list(request):
    sales = (
        Sale.objects.select_related("store", "customer")
        .filter(deleted_at__isnull=True)
        .prefetch_related("items__product", "items__batches__purchase_item__purchase__supplier")
        .order_by("-date", "-id")
    )
    cash_registers = CashRegister.objects.select_related("store").order_by("store__name")
    return render(
        request,
        "sales/sale_list.html",
        {
            "sales": sales,
            "cash_registers": cash_registers,
        },
    )


@login_required
def cash_registers(request):
    registers = CashRegister.objects.select_related("store").order_by("store__name")
    total_cash = registers.aggregate(total=Sum("balance"))["total"] or 0
    return render(
        request,
        "sales/cash_registers.html",
        {
            "cash_registers": registers,
            "total_cash": total_cash,
        },
    )
