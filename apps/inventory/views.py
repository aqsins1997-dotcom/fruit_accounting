from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map

from .forms import PurchaseCreateForm, PurchaseItemCreateForm, PurchaseItemPriceUpdateForm
from .models import Purchase, PurchaseItem


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
        .filter(deleted_at__isnull=True)
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
def purchase_item_price_update(request, pk):
    item = get_object_or_404(
        PurchaseItem.objects.select_related("purchase__supplier", "store", "product").filter(
            purchase__deleted_at__isnull=True
        ),
        pk=pk,
    )

    if request.method == "POST":
        form = PurchaseItemPriceUpdateForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, "Цена закупки успешно обновлена")
            return redirect("inventory:purchase_list")
    else:
        form = PurchaseItemPriceUpdateForm(instance=item)

    return render(
        request,
        "inventory/purchase_price_update.html",
        {
            "form": form,
            "item": item,
        },
    )


@login_required
def stock_list(request):
    stocks = build_product_profitability_rows(group_by_store=True)
    return render(request, "inventory/stock_list.html", {"stocks": stocks})
