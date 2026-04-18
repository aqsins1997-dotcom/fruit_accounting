from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def dashboard(request):
    context = {
        "sections": [
            {
                "title": "Справочники",
                "items": [
                    "Поставщики",
                    "Товары",
                    "Магазины",
                    "Продавцы",
                    "Клиенты",
                ],
            },
            {
                "title": "Операции",
                "items": [
                    "Приходы от поставщиков",
                    "Распределение товара по магазинам",
                    "Продажи за наличные",
                    "Продажи в кредит",
                    "Погашения долгов",
                ],
            },
            {
                "title": "Отчеты",
                "items": [
                    "Ежедневный отчет по магазинам",
                    "Остатки по складам",
                    "Список должников",
                    "Общая статистика владельца",
                ],
            },
        ]
    }
    return render(request, "core/dashboard.html", context)