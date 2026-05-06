import argparse
import os
import socket
import sqlite3
import threading
import time
import webbrowser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "db.sqlite3"
BACKUP_ROOT = Path(
    os.getenv(
        "FRUIT_ACCOUNTING_BACKUP_DIR",
        Path.home() / "Documents" / "fruit_accounting_backups",
    )
)


def find_port(start_port):
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local port found.")


def ensure_local_admin():
    from django.contrib.auth import get_user_model

    username = os.getenv("LOCAL_ADMIN_USERNAME", "admin")
    password = os.getenv("LOCAL_ADMIN_PASSWORD", "admin")

    User = get_user_model()
    user, _ = User.objects.get_or_create(username=username, defaults={"email": ""})
    user.is_active = True
    user.is_staff = True
    user.is_superuser = True
    user.set_password(password)
    user.save()

    return username, password


def open_browser_later(url):
    time.sleep(1.5)
    webbrowser.open(url)


def timestamp():
    return time.strftime("%Y-%m-%d_%H-%M-%S")


def daily_backup_dir():
    path = BACKUP_ROOT / time.strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_sqlite_database(reason):
    if not DATABASE_PATH.exists():
        return None

    destination = daily_backup_dir() / f"db_{reason}_{timestamp()}.sqlite3"

    source = sqlite3.connect(DATABASE_PATH)
    try:
        backup = sqlite3.connect(destination)
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()

    print(f"Database backup saved: {destination}")
    return destination


def backup_json_data(reason, call_command):
    destination = daily_backup_dir() / f"data_{reason}_{timestamp()}.json"

    with destination.open("w", encoding="utf-8") as output:
        call_command(
            "dumpdata",
            natural_foreign=True,
            natural_primary=True,
            exclude=["contenttypes", "auth.permission", "sessions.session"],
            indent=2,
            stdout=output,
        )

    print(f"JSON backup saved: {destination}")
    return destination


def create_backup(reason, call_command=None, include_json=False):
    try:
        backup_sqlite_database(reason)
        if include_json and call_command:
            backup_json_data(reason, call_command)
    except Exception as exc:
        print(f"Backup failed ({reason}): {exc}")


def start_periodic_backups(interval_seconds, call_command):
    stop_event = threading.Event()

    def worker():
        while not stop_event.wait(interval_seconds):
            create_backup("auto", call_command=call_command, include_json=True)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return stop_event


def main():
    parser = argparse.ArgumentParser(description="Run Fruit Business locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--backup-interval", type=int, default=300)
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_DEBUG", "True")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
    os.environ.setdefault("DJANGO_SKIP_STARTUP_TASKS", "1")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{DATABASE_PATH.as_posix()}")

    import django
    from django.core.management import call_command

    create_backup("before_start")

    django.setup()
    call_command("migrate", interactive=False)
    username, password = ensure_local_admin()
    create_backup("after_start", call_command=call_command, include_json=True)

    print()
    print("Fruit Business is ready.")
    print(f"Login: {username}")
    print(f"Password: {password}")
    print(f"Data file: {DATABASE_PATH}")
    print(f"Backups: {BACKUP_ROOT}")

    if args.setup_only:
        return

    port = find_port(args.port)
    url = f"http://{args.host}:{port}/accounts/login/"

    print()
    print(f"Opening: {url}")
    print("Keep this window open while you use the site.")
    print("Press Ctrl+C here to stop the server.")
    print(f"Automatic backups run every {args.backup_interval} seconds.")
    print()

    if not args.no_browser:
        threading.Thread(target=open_browser_later, args=(url,), daemon=True).start()

    backup_stop_event = start_periodic_backups(args.backup_interval, call_command)

    try:
        call_command("runserver", f"{args.host}:{port}", use_reloader=False)
    finally:
        backup_stop_event.set()
        create_backup("shutdown", call_command=call_command, include_json=True)


if __name__ == "__main__":
    main()
