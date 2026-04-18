from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CreditPaymentCreateForm
from .models import Credit


@login_required
def credit_payment_create(request, credit_id):
    credit = get_object_or_404(
        Credit.objects.select_related("customer", "store"),
        pk=credit_id,
    )

    if request.method == "POST":
        form = CreditPaymentCreateForm(request.POST)
        form.instance.credit = credit
        if form.is_valid():
            payment = form.save(commit=False)
            payment.credit = credit
            payment.save()
            messages.success(request, "Оплата успешно сохранена.")
            return redirect("reports:debtor_detail", customer_id=credit.customer_id)
    else:
        form = CreditPaymentCreateForm(
            initial={"date": timezone.now().date()}
        )

    context = {
        "credit": credit,
        "form": form,
    }
    return render(request, "credits/payment_form.html", context)
