"""Idempotent local demonstration seed; credentials only from environment."""
import os
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from shared.security import hash_pin
from core.api_common import password_policy
from core.models import Membership, Plan, Product, Shop, StockMovement, Subscription, Tenant


class Command(BaseCommand):
    help = "Cria admin e empresa de demonstração usando credenciais de variáveis de ambiente"

    def handle(self, *args, **options):
        if not settings.DEBUG and os.environ.get("PDV_ALLOW_DEMO_SEED") != "1":
            raise CommandError("seed_demo bloqueado em produção; use PDV_ALLOW_DEMO_SEED=1 explicitamente")
        admin_password = os.environ.get("PDV_ADMIN_PASSWORD", "")
        demo_password = os.environ.get("PDV_DEMO_PASSWORD", "")
        if not password_policy(admin_password) or not password_policy(demo_password):
            raise CommandError("Configure PDV_ADMIN_PASSWORD e PDV_DEMO_PASSWORD com senhas fortes")
        admin_email = os.environ.get("PDV_ADMIN_EMAIL", "admin@pdv2.local").strip().lower()
        demo_email = os.environ.get("PDV_DEMO_EMAIL", "gestor@pdv2.local").strip().lower()
        demo_pin = os.environ.get("PDV_DEMO_PIN", "")
        if demo_pin:
            try:
                pin_hash = hash_pin(demo_pin)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        else:
            pin_hash = ""
        User = get_user_model()
        with transaction.atomic():
            admin, created = User.objects.get_or_create(username=admin_email, defaults={
                "email": admin_email, "first_name": "Administração", "is_staff": True, "is_superuser": True})
            if not created and not admin.is_superuser:
                raise CommandError("E-mail administrativo já existe sem privilégio de superusuário")
            admin.is_staff = admin.is_superuser = True
            admin.set_password(admin_password)
            admin.save()
            demo, _ = User.objects.get_or_create(username=demo_email, defaults={
                "email": demo_email, "first_name": "Gestor"})
            demo.set_password(demo_password)
            demo.save()
            tenant, _ = Tenant.objects.get_or_create(document="DEMO-LOCAL", defaults={"name": "Mercado Exemplo Maricá"})
            member, _ = Membership.objects.get_or_create(tenant=tenant, user=demo, defaults={
                "name": "Gestor", "role": "owner", "pin_hash": pin_hash})
            if pin_hash:
                member.pin_hash = pin_hash
                member.save(update_fields=["pin_hash"])
            shop, _ = Shop.objects.get_or_create(tenant=tenant, name="Loja principal")
            plan = Plan.objects.get(slug="advanced")
            Subscription.objects.get_or_create(tenant=tenant, defaults={
                "plan": plan, "status": "trial", "trial_ends_at": timezone.now() + timedelta(days=14)})
            examples = [("ARROZ-1KG", "Arroz branco 1 kg", "7891000000001", 899, 20),
                        ("FEIJAO-1KG", "Feijão carioca 1 kg", "7891000000002", 1099, 15),
                        ("LEITE-1L", "Leite integral 1 L", "7891000000003", 599, 24)]
            for sku, name, barcode, price, units in examples:
                product, _ = Product.objects.get_or_create(tenant=tenant, sku=sku, defaults={
                    "name": name, "barcode": barcode, "price_cents": price, "unit": "UN",
                    "stock_minimum_milli": 5000})
                StockMovement.objects.get_or_create(tenant=tenant, source="manual",
                    idempotency_key=f"demo-opening:{sku}", defaults={
                        "shop": shop, "product": product, "delta_milli": units * 1000,
                        "reason": "Saldo inicial de demonstração", "actor": demo})
        self.stdout.write(self.style.SUCCESS(
            f"Demo pronta: {admin_email} (plataforma), {demo_email} (empresa {tenant.id}); senhas não exibidas."))
