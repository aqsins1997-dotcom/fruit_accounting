from django.core.management.base import BaseCommand

from apps.reports.full_audit import run_full_accounting_reconciliation


class Command(BaseCommand):
    help = "Read-only full accounting reconciliation across sales, inventory, suppliers, clients, cash, and reports."

    def handle(self, *args, **options):
        report = run_full_accounting_reconciliation()

        self.stdout.write("READ ONLY FULL ACCOUNTING RECONCILIATION: no data will be changed.")
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

        self.stdout.write("[Summary]")
        self.stdout.write(f"CRITICAL: {report['summary']['critical']}")
        self.stdout.write(f"WARNING: {report['summary']['warning']}")
        self.stdout.write(f"INFO: {report['summary']['info']}")

        if report["has_critical"]:
            raise SystemExit(1)
