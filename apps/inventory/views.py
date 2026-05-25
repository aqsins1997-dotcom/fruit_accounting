from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map

from .forms import (
    PurchaseCreateForm,
    PurchaseItemCreateForm,
    PurchaseItemPriceUpdateForm,
    PurchaseItemQuantityUpdateForm,
)
from .models import Purchase, PurchaseItem


def _action_from_request(request):
    return request.POST.get("action") or "preview"


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

    return render(
        request,
        "inventory/purchase_form.html",
        {"purchase_form": purchase_form, "item_form": item_form},
    )


@login_required
def purchase_list(request):
    purchases = (
        Purchase.objects.select_related("supplier")
        .filter(deleted_at__isnull=True)
        .prefetch_related("items__store", "items__product")
        .order_by("-date", "-id")
    )
    item_ids = [item.id for purchase in purchases for item in purchase.items.all()]
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
    preview = None

    if request.method == "POST":
        form = PurchaseItemPriceUpdateForm(request.POST, instance=item)
        if form.is_valid():
            preview = form.build_preview()
            if _action_from_request(request) == "apply":
                form.save()
                messages.success(request, "Цена закупки успешно обновлена.")
                return redirect("inventory:purchase_list")
    else:
        form = PurchaseItemPriceUpdateForm(instance=item)

    return render(
        request,
        "inventory/purchase_price_update.html",
        {"form": form, "item": item, "preview": preview},
    )


@login_required
def purchase_item_quantity_update(request, pk):
    item_queryset = PurchaseItem.objects.select_related(
        "purchase__supplier",
        "store",
        "product",
    ).filter(purchase__deleted_at__isnull=True)

    if request.method == "POST":
        with transaction.atomic():
            item = get_object_or_404(
                item_queryset.select_for_update(),
                pk=pk,
            )
            preview = None
            form = PurchaseItemQuantityUpdateForm(request.POST, instance=item)
            if form.is_valid():
                preview = form.build_preview()
                if _action_from_request(request) == "apply":
                    form.save()
                    messages.success(request, "Вес закупки успешно обновлен.")
                    return redirect("inventory:purchase_list")
    else:
        item = get_object_or_404(
            item_queryset,
            pk=pk,
        )
        preview = None
        form = PurchaseItemQuantityUpdateForm(instance=item)

    return render(
        request,
        "inventory/purchase_quantity_update.html",
        {
            "form": form,
            "item": item,
            "preview": preview,
            "sold_quantity": form.sold_quantity,
            "current_stock_quantity": form.current_stock_quantity,
            "paid_amount": form.paid_amount,
            "current_purchase_total": form.current_purchase_total_for_store,
            "new_purchase_total": form.new_purchase_total_for_store,
        },
    )


@login_required
def stock_list(request):
    stocks = build_product_profitability_rows(group_by_store=True)
    return render(request, "inventory/stock_list.html", {"stocks": stocks})
