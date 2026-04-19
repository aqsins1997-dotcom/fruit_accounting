from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from .forms import PurchaseCreateForm, PurchaseItemCreateForm
from .models import Purchase, StoreStock


@login_required
def purchase_create(request):
    if request.method == "POST":
        purchase_form = PurchaseCreateForm(request.POST)
        item_form = PurchaseItemCreateForm(request.POST)
        if purchase_form.is_valid() and item_form.is_valid():
            with transaction.atomic():
                purchase = purchase_form.save()
                item = item_form.save(commit=False)
                item.purchase = purchase
                item.save()
            messages.success(request, "Закупка сохранена.")
            return redirect("inventory:purchase_list")
    else:
        purchase_form = PurchaseCreateForm()
        item_form = PurchaseItemCreateForm()

    context = {
        "purchase_form": purchase_form,
        "item_form": item_form,
    }
    return render(request, "inventory/purchase_form.html", context)


@login_required
def purchase_list(request):
    purchases = (
        Purchase.objects.select_related("supplier")
        .prefetch_related("items__store", "items__product")
        .order_by("-date", "-id")
    )
    return render(request, "inventory/purchase_list.html", {"purchases": purchases})


@login_required
def stock_list(request):
    stocks = StoreStock.objects.select_related("store", "product").order_by("store__name", "product__name")
    return render(request, "inventory/stock_list.html", {"stocks": stocks})
