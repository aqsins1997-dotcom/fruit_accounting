from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from .forms import SaleCreateForm, SaleItemCreateForm
from .models import CashRegister, Sale


@login_required
def sale_create(request):
    if request.method == "POST":
        sale_form = SaleCreateForm(request.POST)
        item_form = SaleItemCreateForm(request.POST)
        if sale_form.is_valid() and item_form.is_valid():
            with transaction.atomic():
                sale = sale_form.save()
                item = item_form.save(commit=False)
                item.sale = sale
                item.save()
            messages.success(request, "Продажа сохранена.")
            return redirect("sales:sale_list")
    else:
        sale_form = SaleCreateForm()
        item_form = SaleItemCreateForm()

    context = {
        "sale_form": sale_form,
        "item_form": item_form,
    }
    return render(request, "sales/sale_form.html", context)


@login_required
def sale_list(request):
    sales = Sale.objects.select_related("store", "customer").prefetch_related("items__product").order_by("-date", "-id")
    cash_registers = CashRegister.objects.select_related("store").order_by("store__name")
    return render(
        request,
        "sales/sale_list.html",
        {
            "sales": sales,
            "cash_registers": cash_registers,
        },
    )
