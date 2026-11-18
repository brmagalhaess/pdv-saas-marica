from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import IbptRate
from shared.fiscal.data import UF_CODES
from shared.fiscal.ibpt import parse


class Command(BaseCommand):
    help = ("Carrega a tabela IBPT (Lei 12.741/2012) de uma UF a partir do CSV oficial "
            "(TabelaIBPTax<UF><versão>.csv). Substitui as linhas anteriores da UF.")

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--uf", required=True, help="UF da tabela, ex.: SP")

    def handle(self, *args, csv_path, uf, **options):
        uf = uf.upper()
        if uf not in UF_CODES:
            raise CommandError("UF inválida")
        path = Path(csv_path)
        if not path.is_file():
            raise CommandError("Arquivo não encontrado")
        try:
            rows, errors = parse(path.read_bytes())
        except ValueError as exc:
            raise CommandError(str(exc))
        if not rows:
            raise CommandError("Nenhuma linha de NCM (tipo 0) no arquivo")
        unique = {(row["ncm"], row["ex"]): row for row in rows}
        with transaction.atomic():
            IbptRate.objects.filter(uf=uf).delete()
            IbptRate.objects.bulk_create([
                IbptRate(uf=uf, ncm=row["ncm"], ex=row["ex"], federal_national=row["federal_national"],
                         federal_imported=row["federal_imported"], state=row["state"], municipal=row["municipal"],
                         valid_from=row["valid_from"], valid_until=row["valid_until"], version=row["version"],
                         source=row["source"]) for row in unique.values()], batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(unique)} NCM carregados para {uf}" + (f"; {errors} linha(s) ignorada(s)" if errors else "")))
