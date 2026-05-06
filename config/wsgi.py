"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()


def _run_startup_tasks():
    if os.getenv('DJANGO_SKIP_STARTUP_TASKS', '').lower() in ('1', 'true', 'yes', 'on'):
        return

    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    call_command('migrate', interactive=False, verbosity=0)

    username = os.getenv('DJANGO_SUPERUSER_USERNAME')
    email = os.getenv('DJANGO_SUPERUSER_EMAIL', '')
    password = os.getenv('DJANGO_SUPERUSER_PASSWORD')

    if username and password:
        User = get_user_model()
        user, _ = User.objects.get_or_create(username=username, defaults={'email': email})
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()


_run_startup_tasks()
