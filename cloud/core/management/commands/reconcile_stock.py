from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Product, StockBalance
from core.stock_balance import drift


class Command(BaseCommand):
    help = "Compara o saldo materializado com o histórico de movimentos e, com --fix, corrige divergências."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true", help="Grava o saldo do histórico nas linhas divergentes.")

    def handle(self, *args, fix=False, **options):
        rows = drift()
        for shop_id, product_id, ledger, stored in rows:
            self.stdout.write(f"{shop_id} {product_id}: histórico={ledger} saldo={stored}")
        if fix and rows:
            with transaction.atomic():
                for shop_id, product_id, ledger, _ in rows:
                    tenant_id = Product.objects.filter(pk=product_id).values_list("tenant_id", flat=True).get()
                    StockBalance.objects.update_or_create(shop_id=shop_id, product_id=product_id,
                                                          defaults={"tenant_id": tenant_id, "quantity_milli": ledger})
        self.stdout.write(self.style.SUCCESS(
            f"{len(rows)} divergência(s){' corrigida(s)' if fix and rows else ''}." if rows else "Saldo materializado confere com o histórico."))
        if rows and not fix:
            raise SystemExit(1)
