from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.db.models import Sum

from apps.core.models import Store, Product
from apps.inventory.models import StoreStock
from apps.sales.models import Sale


@login_required
def index(request):
    today = timezone.now().date()

    stores_count = Store.objects.count()
    products_count = Product.objects.count()

    total_stock = StoreStock.objects.aggregate(
        total=Sum("quantity_kg")
    )["total"] or 0

    today_sales = Sale.objects.filter(
        date=today
    ).aggregate(
        total=Sum("total_amount")
    )["total"] or 0

    context = {
        "stats": {
            "stores_count": stores_count,
            "products_count": products_count,
            "total_stock": total_stock,
            "today_sales": today_sales,
            "active_credits": 0,
        }
    }

    return render(request, "dashboard/index.html", context)