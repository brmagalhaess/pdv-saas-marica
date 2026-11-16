#!/usr/bin/env python3
"""Sobe o Caixa Rápido com dados fictícios em http://127.0.0.1:8099.

Serve para o dono do produto ver a tela e para verificação visual. Usa um
diretório temporário próprio, já pareado com uma empresa de mentira: nunca lê
nem grava o banco real do terminal e nunca fala com a nuvem.

    python scripts/demo_terminal.py            # online simulado
    python scripts/demo_terminal.py --offline  # simula queda da internet
    python scripts/demo_terminal.py --revisao  # já abre com registros "em revisão"
    python scripts/demo_terminal.py --licenca-5-dias  # licença offline vencendo em 5 dias
    python scripts/demo_terminal.py --atualizacao     # nova versão baixada, pronta para instalar
"""
import argparse
import json
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.licensing import generate_keypair, payload_digest, sign_lease  # noqa: E402
from shared.security import hash_pin  # noqa: E402
from terminal import sync  # noqa: E402
from terminal.app import create_app  # noqa: E402

CASHIER_PIN = "135790"
MANAGER_PIN = "246801"


def ean13(body):
    digits = [int(char) for char in body]
    check = (10 - sum(digit * (3 if index % 2 else 1) for index, digit in enumerate(digits)) % 10) % 10
    return body + str(check)


def demo_catalog():
    rows = [
        # name, unit, category, price, stock, image, scale code
        ("Banana prata", "KG", "Hortifruti", 599, 40000, "produce", "00101"),
        ("Tomate italiano", "KG", "Hortifruti", 899, 25000, "produce", "00102"),
        ("Batata lavada", "KG", "Hortifruti", 549, 60000, "produce", "00103"),
        ("Maçã gala", "KG", "Hortifruti", 1090, 18000, "produce", "00104"),
        ("Pão francês", "KG", "Padaria e frios", 1690, 30000, "bakery", "00201"),
        ("Queijo mussarela fatiado", "KG", "Padaria e frios", 4990, 12000, "bakery", "00202"),
        ("Presunto cozido fatiado", "KG", "Padaria e frios", 3890, 9000, "bakery", "00203"),
        ("Leite integral 1 L", "UN", "Padaria e frios", 549, 96000, "bakery", None),
        ("Arroz tipo 1 5 kg", "UN", "Mercearia", 2790, 40000, "grocery", None),
        ("Feijão carioca 1 kg", "UN", "Mercearia", 849, 60000, "grocery", None),
        ("Açúcar refinado 1 kg", "UN", "Mercearia", 499, 50000, "grocery", None),
        ("Café torrado e moído 500 g", "UN", "Mercearia", 1890, 30000, "grocery", None),
        ("Óleo de soja 900 ml", "UN", "Mercearia", 789, 48000, "grocery", None),
        ("Macarrão espaguete 500 g", "UN", "Mercearia", 459, 70000, "grocery", None),
        ("Sabão em pó 800 g", "UN", "Mercearia", 1290, 0, "cleaning", None),
        ("Água mineral 500 ml", "UN", "Bebidas", 250, 120000, "beverages", None),
        ("Suco de laranja integral 1 L", "UN", "Bebidas", 1290, 24000, "beverages", None),
        ("Cerveja pilsen lata 350 ml", "UN", "Bebidas", 449, 144000, "beverages", None),
    ]
    products = []
    for index, (name, unit, category, price, stock, image, scale_code) in enumerate(rows, start=1):
        products.append({"id": str(uuid.uuid4()), "sku": f"P{index:03d}", "barcode": ean13(f"78900000{index:04d}"),
                         "name": name, "unit": unit, "category": category, "price_cents": price,
                         "quantity_milli": stock, "scale_code": scale_code, "active": True,
                         "image_url": f"/assets/product-images/{image}.svg"})
    group_id = str(uuid.uuid4())
    for offset, (variant, price, stock) in enumerate((("Lata 350 ml", 399, 96000), ("Garrafa 600 ml", 549, 48000),
                                                     ("Garrafa 2 L", 899, 36000)), start=len(rows) + 1):
        products.append({"id": str(uuid.uuid4()), "sku": f"P{offset:03d}", "barcode": ean13(f"78900000{offset:04d}"),
                         "name": f"Refrigerante guaraná {variant}", "unit": "UN", "category": "Bebidas",
                         "price_cents": price, "quantity_milli": stock, "active": True,
                         "group_id": group_id, "group_name": "Refrigerante guaraná", "variant": variant,
                         "image_url": "/assets/product-images/beverages.svg"})
    return products


def seed(store, expiring=False):
    """Pair and bootstrap the same way terminal/tests/test_terminal.py does."""
    tenant_id, shop_id, terminal_id = (str(uuid.uuid4()) for _ in range(3))
    private, public = generate_keypair()
    operators = [
        {"id": str(uuid.uuid4()), "name": "Carla (caixa)", "role": "cashier", "pin_hash": hash_pin(CASHIER_PIN)},
        {"id": str(uuid.uuid4()), "name": "Marcos (gerente)", "role": "manager", "pin_hash": hash_pin(MANAGER_PIN)},
    ]
    products = demo_catalog()
    water = next(product for product in products if product["name"].startswith("Água mineral"))
    now = int(time.time())
    # --licenca-5-dias: a lease issued a day ago (the terminal has been offline) with 5 days left.
    issued, expires = (now - 24 * 3600, now + 5 * 24 * 3600 + 3600) if expiring else (now - 5, now + 72 * 3600)
    claims = {"schema": 1, "tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal_id,
              "device_id": store.identity()["device_id"], "issued_at": issued, "expires_at": expires,
              "features": ["pdv"], "status": "active", "operators_hash": payload_digest(operators)}
    lease = sign_lease(claims, private)
    names = {"tenant": {"name": "Mercadinho Exemplo (demonstração)"}, "shop": {"name": "Loja Centro"},
             "terminal": {"name": "Caixa 01"}, "license_hint": "DEMO",
             "issuer": {"trade_name": "Mercadinho Exemplo", "legal_name": "Empresa fictícia para demonstração"}}
    store.set_pairing({"token": "demo-" + "0" * 43, "tenant_id": tenant_id, "shop_id": shop_id,
                       "terminal_id": terminal_id, "lease": lease, "public_key": public, **names},
                      "http://127.0.0.1:9")
    store.apply_bootstrap({
        "products": products, "operators": operators, "lease": lease, "public_key": public, **names,
        "customers": [
            {"id": str(uuid.uuid4()), "name": "Maria Aparecida (fiado)", "document": "",
             "credit_limit_cents": 30000, "open_credit_cents": 8000, "store_credit_cents": 0},
            {"id": str(uuid.uuid4()), "name": "João Batista", "document": "",
             "credit_limit_cents": 0, "open_credit_cents": 0, "store_credit_cents": 2500},
        ],
        "promotions": [{"id": str(uuid.uuid4()), "name": "Água leve 3 pague 2", "kind": "buy_x_pay_y",
                        "product_id": water["id"], "buy_quantity": 3, "pay_quantity": 2, "active": True}],
        "payment_methods": [
            {"id": "cash", "kind": "cash", "name": "Dinheiro", "active": True},
            {"id": "pix", "kind": "pix", "name": "Pix", "active": True},
            {"id": "card_debit", "kind": "card_debit", "name": "Débito", "active": True},
            {"id": "card_credit", "kind": "card_credit", "name": "Crédito", "active": True},
            {"id": "credit_account", "kind": "credit_account", "name": "A prazo (fiado)", "active": True},
            {"id": "store_credit", "kind": "store_credit", "name": "Crédito na loja", "active": True},
            {"id": str(uuid.uuid4()), "kind": "other", "name": "Vale-alimentação", "active": True},
        ],
    })
    return products


def seed_review(store):
    """One event kept for review by the (fake) cloud and one catalog row the terminal skipped."""
    cashier = next(operator for operator in store.operators() if operator["role"] == "cashier")
    store.open_cash(cashier["id"], 10000)
    events = store.pending_events()
    store.record_quarantined([{"event_id": events[0]["event_id"], "reason": "rejected_event",
                               "message": "Já existe um caixa aberto para este terminal no servidor"}], events)
    store.ack_events([event["event_id"] for event in events])
    with store.transaction() as conn:
        skipped = [{"kind": "product", "id": str(uuid.uuid4()), "error": "Unidade de produto inválida"}]
        store._set_meta(conn, "catalog_skipped", json.dumps(skipped, ensure_ascii=False))


def simulate_update(store, version="2.1.0"):
    """Stand-in for terminal/updater.py: a verified package already downloaded, nothing is installed."""
    manifest = {"schema": 1, "purpose": "pdv2-release", "component": "terminal", "channel": "stable",
                "version": version, "kind": "bundle", "url": "https://atualizacoes.exemplo.invalid/pdv2.zip",
                "sha256": "0" * 64, "size_bytes": 48 * 1024 * 1024, "notes": "Demonstração: melhorias no caixa"}
    store.set_release_result(manifest)
    current = {"state": "staged"}

    def status(target):
        cash_open = bool(target.cash_state())
        return {"current_version": sync.VERSION, "available": manifest, "staged_version": version,
                "state": current["state"], "error": "", "can_apply": current["state"] == "staged" and not cash_open,
                "blocked_reason": "Feche o caixa para atualizar" if cash_open else ""}

    def request_apply(target):
        current["state"] = "applying"  # the demo never restarts or replaces anything
        return status(target)

    sync.updater = types.SimpleNamespace(status=status, request_apply=request_apply,
                                         auto_step=lambda target, online: None)


def simulate_cloud(store, offline, stop):
    """No real cloud here: acknowledge the outbox locally, or report an outage with --offline."""
    while not stop.is_set():
        try:
            if offline:
                store.set_heartbeat_result("Sem conexão com o servidor (simulação)")
                if store.pending_events(1):
                    store.set_sync_result("Sem conexão com o servidor (simulação)")
            else:
                events = store.pending_events(50)
                if events:
                    store.ack_events([event["event_id"] for event in events])
                    store.set_sync_result()
                store.set_heartbeat_result()
        except Exception as exc:  # a demo must keep serving the screen
            print(f"[demo] simulação de nuvem: {exc}", flush=True)
        stop.wait(8)


def main():
    parser = argparse.ArgumentParser(description="Caixa Rápido PDV2 com dados fictícios")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--offline", action="store_true", help="simula o caixa sem internet (fila pendente)")
    parser.add_argument("--revisao", action="store_true",
                        help="abre o caixa com um registro em revisão e um item de catálogo ignorado")
    parser.add_argument("--licenca-5-dias", dest="expiring", action="store_true",
                        help="simula a licença offline vencendo em 5 dias (aviso na barra e em Sistema)")
    parser.add_argument("--atualizacao", action="store_true",
                        help="simula uma nova versão já baixada, pronta para instalar (cartão Atualização)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pdv2-demo-") as data_dir:
        app = create_app({"DATA_DIR": data_dir, "DB_PATH": Path(data_dir) / "terminal.sqlite3", "START_WORKER": False,
                          "TEMPLATES_AUTO_RELOAD": True})
        store = app.extensions["terminal_store"]
        products = seed(store, expiring=args.expiring)
        if args.revisao:
            seed_review(store)
        if args.atualizacao:
            simulate_update(store)
        receipts = Path(data_dir) / "cupons"
        receipts.mkdir()
        store.save_printer("receipt", "file", str(receipts), "escpos80")
        stop = threading.Event()
        threading.Thread(target=simulate_cloud, args=(store, args.offline, stop), daemon=True).start()
        banana = next(product for product in products if product["name"] == "Banana prata")
        rice = next(product for product in products if product["name"].startswith("Arroz"))
        print("\n".join([
            "",
            "PDV2 por CrownTech · Caixa Rápido (demonstração com dados fictícios)",
            f"  Abra:            http://127.0.0.1:{args.port}",
            f"  Caixa:           Carla (caixa)     PIN {CASHIER_PIN}",
            f"  Gerente:         Marcos (gerente)  PIN {MANAGER_PIN}",
            f"  Código de teste: {rice['barcode']} ({rice['name']}) · tente também 3*{rice['barcode']}",
            f"  Etiqueta balança: {ean13('2' + banana['scale_code'] + '01250' + '0')} ({banana['name']}, 1,250 kg)",
            "  Promoção:        Água mineral 500 ml · leve 3 pague 2",
            f"  Conexão:         {'offline simulado (fila pendente)' if args.offline else 'online simulado'}",
            *(["  Licença:         vence em 5 dias (aviso na barra de status e em Sistema)"] if args.expiring else []),
            *(["  Atualização:     versão 2.1.0 pronta (Sistema > Atualização; feche o caixa para instalar)"]
              if args.atualizacao else []),
            f"  Dados em:        {data_dir} (apagados ao encerrar)",
            "  Encerrar:        Ctrl+C",
            "",
        ]), flush=True)
        try:
            from waitress import serve
            serve(app, host="127.0.0.1", port=args.port, threads=6)
        except ImportError:
            app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()


if __name__ == "__main__":
    main()
