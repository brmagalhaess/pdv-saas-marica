"""Interface de demonstração do caixa local: python -m local_pdv --help."""

import argparse
import json
import os
import sys

from .core import LocalPDV, PDVError


def _pair(raw, label):
    try:
        key, value = raw.rsplit(":", 1)
        return key, int(value)
    except ValueError as exc:
        raise PDVError(f"{label}: use CODIGO:VALOR") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Protótipo de PDV local sem internet")
    parser.add_argument("--db", default=os.environ.get("PDV_LOCAL_DB", "local_pdv.sqlite3"), help="Arquivo SQLite")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Vincula um banco a empresa, loja e terminal")
    init.add_argument("--tenant", required=True)
    init.add_argument("--shop", required=True)
    init.add_argument("--terminal", required=True)
    product = commands.add_parser("product", help="Carrega produto no catálogo local")
    product.add_argument("--id", required=True)
    product.add_argument("--name", required=True)
    product.add_argument("--price", type=int, required=True, help="Centavos")
    product.add_argument("--stock", type=int, default=0, help="Unidades iniciais; ignorado em produto existente")
    product.add_argument("--barcode")
    adjust = commands.add_parser("adjust", help="Ajusta estoque com motivo")
    adjust.add_argument("--id", required=True)
    adjust.add_argument("--delta", type=int, required=True)
    adjust.add_argument("--reason", required=True)
    adjust.add_argument("--request", required=True)
    opening = commands.add_parser("open", help="Abre caixa")
    opening.add_argument("--opening", type=int, required=True, help="Centavos")
    sell = commands.add_parser("sell", help="Registra uma venda local")
    sell.add_argument("--request", required=True, help="Identificador único gerado antes da tentativa")
    sell.add_argument("--item", action="append", required=True, help="CODIGO:QUANTIDADE; repetir para cada item")
    sell.add_argument("--payment", action="append", required=True, help="cash|pix|card:CENTAVOS")
    closing = commands.add_parser("close", help="Fecha caixa")
    closing.add_argument("--counted", type=int, required=True, help="Dinheiro contado em centavos")
    commands.add_parser("status", help="Mostra integridade, vendas e eventos pendentes")
    commands.add_parser("products", help="Lista catálogo local")
    lookup = commands.add_parser("lookup", help="Busca por código interno ou de barras")
    lookup.add_argument("--code", required=True)
    commands.add_parser("outbox", help="Lista eventos pendentes para futura sincronização")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            pdv = LocalPDV(args.db, args.tenant, args.shop, args.terminal)
            result = pdv.status()
        else:
            pdv = LocalPDV(args.db)
            if args.command == "product":
                result = pdv.upsert_product(args.id, args.name, args.price, args.stock, args.barcode)
            elif args.command == "adjust":
                result = pdv.adjust_stock(args.id, args.delta, args.reason, args.request)
            elif args.command == "open":
                result = pdv.open_cash(args.opening)
            elif args.command == "sell":
                items = [{"product_id": key, "quantity": value} for key, value in (_pair(x, "Item") for x in args.item)]
                payments = [{"method": key, "amount_cents": value} for key, value in (_pair(x, "Pagamento") for x in args.payment)]
                result = pdv.complete_sale(args.request, items, payments)
            elif args.command == "close":
                result = pdv.close_cash(args.counted)
            elif args.command == "status":
                result = pdv.status()
            elif args.command == "products":
                result = pdv.products()
            elif args.command == "lookup":
                result = pdv.lookup_product(args.code)
            else:
                result = pdv.pending_events()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PDVError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
