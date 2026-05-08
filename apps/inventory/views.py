from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map

from .forms import PurchaseCreateForm, PurchaseItemCreateForm
from .models import Purchase, StoreStock


def _profitability_by_store_product():
    return {
        (row["store_id"], row["product_id"]): row
        for row in build_product_profitability_rows(group_by_store=True)
    }


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
    item_ids = [
        item.id
        for purchase in purchases
        for item in purchase.items.all()
    ]
    profitability_map = build_purchase_item_profitability_map(purchase_item_ids=item_ids)
    for purchase in purchases:
        for item in purchase.items.all():
            item.profitability = profitability_map.get(item.id)

    return render(request, "inventory/purchase_list.html", {"purchases": purchases})


@login_required
def stock_list(request):
    stocks = StoreStock.objects.select_related("store", "product").order_by("store__name", "product__name")
    profitability_map = _profitability_by_store_product()
    for stock in stocks:
        stock.profitability = profitability_map.get((stock.store_id, stock.product_id))

    return render(request, "inventory/stock_list.html", {"stocks": stocks})
