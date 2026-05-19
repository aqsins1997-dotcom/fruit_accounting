#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
echo "=== START SALEITEM 14 DRY RUN ==="
python manage.py repair_saleitem_allocations --sale-item-id 14 || true
echo "=== END SALEITEM 14 DRY RUN ==="
echo "=== START ACCOUNTING AUDIT ==="
python manage.py audit_accounting_integrity || true
echo "=== END ACCOUNTING AUDIT ==="

python manage.py shell -c "
import os
from django.contrib.auth import get_user_model

username = os.getenv('DJANGO_SUPERUSER_USERNAME')
email = os.getenv('DJANGO_SUPERUSER_EMAIL')
password = os.getenv('DJANGO_SUPERUSER_PASSWORD')

if username and email and password:
    User = get_user_model()
    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(username, email, password)
"
