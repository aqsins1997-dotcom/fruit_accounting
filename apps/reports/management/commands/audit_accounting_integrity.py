from django.core.management.base import BaseCommand

from apps.reports.audit import run_accounting_audit


class Command(BaseCommand):
    help = "Read-only audit of accounting integrity across stock, debts, cash, expenses, and reports."

    def handle(self, *args, **options):
        report = run_accounting_audit()

        self.stdout.write("READ ONLY AUDIT: no data will be changed.")
        self.stdout.write("")

        for section in report["sections"]:
            self.stdout.write(f"[{section['name']}]")
            if section["critical"]:
                self.stdout.write("  CRITICAL:")
                for message in section["critical"]:
                    self.stdout.write(f"    - {message}")
            if section["warning"]:
                self.stdout.write("  WARNING:")
                for message in section["warning"]:
                    self.stdout.write(f"    - {message}")
            if section["info"]:
                self.stdout.write("  INFO:")
                for message in section["info"]:
                    self.stdout.write(f"    - {message}")
            self.stdout.write("")

        self.stdout.write("SUMMARY")
        self.stdout.write(f"  CRITICAL: {report['summary']['critical']}")
        self.stdout.write(f"  WARNING:  {report['summary']['warning']}")
        self.stdout.write(f"  INFO:     {report['summary']['info']}")

        if report["has_critical"]:
            raise SystemExit(1)
