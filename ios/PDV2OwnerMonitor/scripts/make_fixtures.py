"""Grava as fixtures JSON do contrato (exemplos da seção 6) para os testes iOS."""
import json
import os
import sys

out = sys.argv[1]
os.makedirs(out, exist_ok=True)

SHOP1 = {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"}
SHOP2 = {"id": "a1b2c3d4-0002-4000-8000-000000000002", "name": "Loja Bairro"}
TENANT = {"id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", "name": "Mercado Bom Preço"}
ANA = {"id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br"}
BRUNO = {"id": 1, "name": "Bruno", "email": "admin@crowntech.com.br"}
ALL = ["sales", "cash", "stock", "credit", "terminals"]

DEVICE = {"id": "d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", "platform": "ios", "name": "iPhone", "model": "iPhone16,2",
          "os_version": "iOS 26.1", "app_version": "1.0.0 (1)", "created_at": "2026-11-16T14:03:12-03:00",
          "last_seen_at": "2026-11-16T14:03:12-03:00", "current": True}
TENANT_ACCESS = {"id": TENANT["id"], "name": TENANT["name"], "all_shops": False, "shops": [SHOP1],
                 "permissions": ALL, "access_expires_on": None}
TOKENS = {"access_token": "pa_Q2xhdmUtZGUtdGVzdGUtYWNlc3NvLTEyMzQ1Njc4OTA",
          "access_expires_at": "2026-11-16T14:18:12-03:00", "access_expires_in": 900,
          "refresh_token": "pr_Zm9vYmFyLXJlZnJlc2gtdG9rZW4tZGUtdGVzdGUtMTIzNDU2Nzg5MC1hYmNkZWY",
          "refresh_expires_at": "2027-01-15T14:03:12-03:00"}
LINKS = {"privacy_url": "https://pdv2.crowntech.com.br/privacidade/",
         "support_url": "https://pdv2.crowntech.com.br/suporte/",
         "account_deletion_url": "https://pdv2.crowntech.com.br/excluir-conta/"}
ME = {"user": ANA, "device": DEVICE, "platform": None, "tenants": [TENANT_ACCESS], "links": LINKS,
      "server_time": "2026-11-16T14:03:12-03:00"}
ME_PLATFORM = {"user": BRUNO, "device": DEVICE,
               "platform": {"enabled": True, "superuser": True, "can_manage_access": True,
                            "roles": ["platform_billing", "platform_ops", "platform_support"]},
               "tenants": [], "links": LINKS, "server_time": "2026-11-16T14:03:12-03:00"}

F = {}
F["me"] = ME
F["me_platform"] = ME_PLATFORM
F["login_ok"] = {"status": "ok", "tokens": TOKENS, "me": ME}
F["login_mfa"] = {"status": "mfa_required", "mfa_token": "pm_aGVsbG8tbWZhLXRva2VuLWRlLXRlc3RlLTEyMzQ1",
                  "mfa_expires_at": "2026-11-16T14:13:12-03:00",
                  "user": {"name": "Bruno", "email": "admin@crowntech.com.br"}}
F["mfa_ok"] = {"status": "ok", "tokens": TOKENS, "me": ME_PLATFORM}
F["refresh"] = {"tokens": TOKENS}
F["login_request"] = {"email": "ana@mercadobompreco.com.br", "password": "Senha-forte-1",
                      "device": {"platform": "ios", "name": "iPhone", "model": "iPhone16,2",
                                 "os_version": "iOS 26.1", "app_version": "1.0.0 (1)"}}
F["tenants"] = {"results": [TENANT_ACCESS], "next_cursor": None}
F["dashboard"] = {
    "generated_at": "2026-11-16T14:03:12-03:00", "tenant": TENANT, "shop": None, "shops": [SHOP1, SHOP2],
    "period": {"key": "today", "label": "Hoje", "date_from": "2026-11-16", "date_to": "2026-11-16",
               "from": "2026-11-16T00:00:00-03:00", "to": "2026-11-17T00:00:00-03:00"},
    "permissions": ALL,
    "sales": {"count": 42, "revenue_cents": 352000, "average_ticket_cents": 8380, "cancelled_count": 2,
              "cancelled_cents": 5400, "returns_cents": 1200, "net_revenue_cents": 350800},
    "payments": {"cash_cents": 98000, "pix_cents": 150000, "card_credit_cents": 60000, "card_debit_cents": 30000,
                 "other_cents": 4000, "received_total_cents": 342000, "credit_account_cents": 8000,
                 "store_credit_cents": 2000, "credit_received_cents": 5000},
    "series": {"kind": "hour", "points": [{"key": "00", "label": "00h", "count": 0, "revenue_cents": 0},
                                          {"key": "14", "label": "14h", "count": 6, "revenue_cents": 51000}]},
    "cash": {"open_count": 2, "closed_count": 3, "shortage_count": 1, "shortage_cents": -1500},
    "stock": {"zeroed_count": 3, "below_minimum_count": 7},
    "credit": {"open_cents": 120000, "overdue_cents": 30000, "overdue_customers": 4},
    "terminals": {"total": 3, "online": 2, "offline": 1, "unpaired": 0, "online_window_seconds": 300,
                  "conflicts_open": 1}}
F["dashboard_restricted"] = {
    "generated_at": "2026-11-16T14:03:12-03:00", "tenant": TENANT, "shop": SHOP1, "shops": [SHOP1],
    "period": {"key": "7d", "label": "7 dias", "date_from": "2026-11-10", "date_to": "2026-11-16",
               "from": "2026-11-10T00:00:00-03:00", "to": "2026-11-17T00:00:00-03:00"},
    "permissions": ["sales"],
    "sales": {"count": 0, "revenue_cents": 0, "average_ticket_cents": 0, "cancelled_count": 0,
              "cancelled_cents": 0, "returns_cents": 0, "net_revenue_cents": 0},
    "payments": {"cash_cents": 0, "pix_cents": 0, "card_credit_cents": 0, "card_debit_cents": 0,
                 "other_cents": 0, "received_total_cents": 0, "credit_account_cents": 0,
                 "store_credit_cents": 0, "credit_received_cents": 0},
    "series": {"kind": "day", "points": [{"key": "2026-11-10", "label": "10/11", "count": 0, "revenue_cents": 0}]},
    "cash": None, "stock": None, "credit": None, "terminals": None}
F["sales"] = {"results": [{"id": "5c0e9a1b-2d3f-4e5a-8b7c-1d2e3f4a5b6c", "created_at": "2026-11-16T13:58:40-03:00",
                           "shop": SHOP1, "terminal_name": "Caixa 1", "station_name": "", "operator_name": "Carla",
                           "customer_name": "", "total_cents": 4590, "status": "completed", "items_count": 2,
                           "payment_methods": ["pix"]}], "next_cursor": None}
F["sale_detail"] = {
    "id": "5c0e9a1b-2d3f-4e5a-8b7c-1d2e3f4a5b6c", "created_at": "2026-11-16T13:58:40-03:00",
    "received_at": "2026-11-16T13:58:43-03:00", "status": "completed", "shop": SHOP1,
    "terminal_name": "Caixa 1", "station_name": "", "operator_name": "Carla", "seller_name": "", "customer_name": "",
    "gross_cents": 4800, "promo_discount_cents": 110, "discount_cents": 100, "total_cents": 4590, "change_cents": 0,
    "items": [{"line": 1, "name": "Arroz 5 kg", "unit": "UN", "quantity_milli": 1000, "unit_price_cents": 2890,
               "gross_cents": 2890, "promo_discount_cents": 0, "discount_cents": 100, "line_total_cents": 2790},
              {"line": 2, "name": "Feijão 1 kg", "unit": "UN", "quantity_milli": 2000, "unit_price_cents": 955,
               "gross_cents": 1910, "promo_discount_cents": 110, "discount_cents": 0, "line_total_cents": 1800}],
    "payments": [{"method": "pix", "label": "Pix", "amount_cents": 4590}],
    "returns": [], "cancelled_at": None, "cancellation_reason": ""}
F["sale_detail_refunded"] = dict(F["sale_detail"], status="refunded", cancelled_at="2026-11-16T15:00:00-03:00",
                                 cancellation_reason="Cliente desistiu",
                                 returns=[{"id": "8a7b6c5d-4e3f-4a2b-9c1d-0e9f8a7b6c5d",
                                           "created_at": "2026-11-16T15:00:00-03:00", "total_cents": 4590,
                                           "refund_method": "cash", "refund_label": "Dinheiro",
                                           "reason": "Produto com defeito"}])
CASH = {"id": "c4a1e2b3-7d8f-4a9b-8c0d-1e2f3a4b5c6d", "shop": SHOP1, "terminal_name": "Caixa 1",
        "station_name": "", "operator_name": "Carla", "status": "open", "opened_at": "2026-11-16T08:01:00-03:00",
        "closed_at": None, "opening_cents": 20000, "sales_count": 18, "sales_total_cents": 154000,
        "cash_net_cents": 43000, "supplied_cents": 0, "withdrawn_cents": 10000, "expected_cents": None,
        "counted_cents": None, "difference_cents": None, "incomplete": False}
CASH_CLOSED = dict(CASH, id="d5b2f3c4-8e9a-4b0c-9d1e-2f3a4b5c6d7e", terminal_name="Caixa 2", status="closed",
                   closed_at="2026-11-16T12:00:00-03:00", expected_cents=63000, counted_cents=61500,
                   difference_cents=-1500)
F["cash_sessions"] = {"results": [CASH], "next_cursor": None}
F["cash_sessions_closed"] = {"results": [CASH_CLOSED], "next_cursor": None}
F["cash_session_detail"] = dict(CASH, closed_by_name="", supervisor_name="", credit_received_cents=0,
                                by_method=[{"method": "cash", "label": "Dinheiro", "amount_cents": 43000},
                                           {"method": "pix", "label": "Pix", "amount_cents": 111000}],
                                movements=[{"id": "e1f2a3b4-1111-4c2d-8e3f-0a1b2c3d4e5f", "kind": "withdrawn",
                                            "label": "Sangria", "amount_cents": 10000,
                                            "reason": "Depósito no banco", "operator_name": "Carla",
                                            "created_at": "2026-11-16T12:30:00-03:00"}])
F["stock_alerts"] = {"counts": {"zeroed": 3, "below_minimum": 7},
                     "results": [{"shop": SHOP1, "product_id": "0f1e2d3c-4b5a-4968-8776-655443322110",
                                  "name": "Leite integral 1 L", "sku": "7891000100103", "unit": "UN",
                                  "quantity_milli": 0, "minimum_milli": 12000, "status": "zeroed"}],
                     "next_cursor": None}
F["credit"] = {"totals": {"open_cents": 120000, "overdue_cents": 30000, "customers": 12, "overdue_customers": 4},
               "results": [{"customer_id": "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d", "name": "João da Silva",
                            "phone": "21999990000", "open_cents": 15000, "overdue_cents": 9000,
                            "oldest_due_date": "2026-10-20", "entries_count": 3}],
               "next_cursor": None}
F["terminals"] = {"online_window_seconds": 300,
                  "results": [{"id": "7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d", "name": "Caixa 1", "shop": SHOP1,
                               "status": "online", "version": "2.1.0", "last_seen_at": "2026-11-16T14:02:50-03:00",
                               "pending_events": 0, "error": ""}],
                  "next_cursor": None}
F["conflicts"] = {"results": [{"id": "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f", "created_at": "2026-11-16T10:15:00-03:00",
                               "shop": SHOP1, "terminal_name": "Caixa 2", "reason": "negative_stock",
                               "reason_text": "Estoque negativo", "status": "open", "status_text": "Em aberto"}],
                  "next_cursor": None}
F["devices"] = {"results": [DEVICE], "next_cursor": None}
F["ok"] = {"ok": True}
F["deletion_request"] = {"status": "received",
                         "message": "Recebemos seu pedido. Seu login será desativado e seus dados pessoais apagados "
                                    "em até 15 dias. Você vai receber a confirmação por e-mail."}
F["platform_tenants"] = {"results": [{"id": TENANT["id"], "name": TENANT["name"], "active": True,
                                      "subscription_status": "active", "subscription_status_text": "Ativa",
                                      "shops_count": 2, "terminals_total": 3, "terminals_offline": 1,
                                      "conflicts_open": 1, "last_sale_at": "2026-11-16T13:58:40-03:00",
                                      "last_seen_at": "2026-11-16T14:02:50-03:00", "health": "attention",
                                      "health_text": "Precisa de atenção"}],
                         "next_cursor": "50"}
ACCESS = {"id": "6e5d4c3b-2a19-4f8e-9d7c-6b5a49382716", "scope": "tenant", "user": ANA, "tenant": TENANT,
          "all_shops": False, "shops": [SHOP1], "permissions": ALL, "reason": "Dona da empresa pediu acesso ao app",
          "granted_by": BRUNO, "granted_at": "2026-11-16T14:00:00-03:00", "expires_on": None, "status": "active",
          "status_text": "Ativo", "revoked_at": None, "revoked_by": None, "revoke_reason": "", "devices_active": 1}
ACCESS_PLATFORM = {"id": "7f6e5d4c-3b2a-4190-8e7d-6c5b4a392817", "scope": "platform",
                   "user": {"id": 7, "name": "Carlos Suporte", "email": "carlos@crowntech.com.br"},
                   "tenant": None, "all_shops": True, "shops": [], "permissions": ALL, "reason": "Atendimento",
                   "granted_by": BRUNO, "granted_at": "2026-11-10T09:00:00-03:00", "expires_on": "2026-12-31",
                   "status": "revoked", "status_text": "Revogado", "revoked_at": "2026-11-15T18:00:00-03:00",
                   "revoked_by": BRUNO, "revoke_reason": "Saiu da equipe", "devices_active": 0}
F["platform_access"] = {"results": [ACCESS, ACCESS_PLATFORM], "next_cursor": None}
F["access"] = ACCESS
F["access_grant_request"] = {"scope": "tenant", "user_id": 12, "tenant_id": TENANT["id"], "shop_ids": [],
                             "permissions": ALL, "expires_on": None, "reason": "Dona da empresa pediu acesso ao app"}
F["access_grant_request_platform"] = {"scope": "platform", "user_id": 7, "expires_on": None, "reason": "Atendimento"}
F["access_options"] = {"tenant": TENANT, "shops": [SHOP1],
                       "members": [{"user_id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br",
                                    "role": "owner", "role_text": "Proprietário", "has_active_access": False}]}
F["access_options_platform"] = {"tenant": None, "shops": [],
                                "members": [{"user_id": 7, "name": "Carlos Suporte", "email": "carlos@crowntech.com.br",
                                             "role": "platform", "role_text": "Equipe CrownTech",
                                             "has_active_access": False}]}
PDEVICE = {"id": "d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", "platform": "android", "name": "Samsung SM-S918B",
           "model": "SM-S918B", "os_version": "Android 15", "app_version": "1.0.0 (1)",
           "created_at": "2026-11-16T14:03:12-03:00", "last_seen_at": "2026-11-16T14:03:12-03:00",
           "last_ip": "177.10.20.30", "user": ANA, "status": "active", "revoked_at": None, "revoke_reason": ""}
F["platform_devices"] = {"results": [PDEVICE], "next_cursor": None}
F["platform_device"] = PDEVICE
F["error_rate_limited"] = {"error": "Muitas tentativas. Aguarde alguns minutos e tente de novo.",
                           "code": "rate_limited", "retry_after_seconds": 600}
F["error_access_exists"] = {"error": "Essa pessoa já tem acesso liberado a esta empresa.", "code": "access_exists",
                            "access_id": ACCESS["id"]}
F["error_token_expired"] = {"error": "Sessão expirada.", "code": "token_expired"}

for name, value in F.items():
    with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

# Mesmo conteúdo embutido em Swift para os Previews (dados fictícios).
swift_out = sys.argv[2]
lines = ["// Gerado a partir de PDV2GestaoTests/Fixtures (exemplos do contrato). Só para Previews.",
         "// Não edite à mão: rode o script de fixtures e copie de novo.", "",
         "enum PreviewFixtures {"]
for name, value in F.items():
    camel = "".join(p.capitalize() if i else p for i, p in enumerate(name.split("_")))
    lines.append(f'    static let {camel} = #"""')
    lines.append(json.dumps(value, ensure_ascii=False))
    lines.append('"""#')
lines.append("}")
with open(swift_out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(len(F), "fixtures")
