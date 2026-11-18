"""Área de balança do balcão: pesar, imprimir a etiqueta de pesado e registrar a conferência.

Nada aqui mexe em estoque nem em caixa: o estoque baixa quando o caixa vende a etiqueta.
As rotas ficam neste arquivo e são ligadas por terminal/app.py (register_routes).
"""

import json
import uuid
from datetime import datetime

from flask import jsonify, request

from .labels import (BARCODE_MODES, DEFAULT_SIZE, LABEL_LANGUAGES, LABEL_SIZES, LANGUAGE_NAMES, MAX_COPIES,
                     LabelError, decode_weighed, label_fields, weighed_label_bytes)
from .store import MANAGER_ROLES, SELL_ROLES, TerminalError, _fold, _like, _now_ms

# Quem pesa e etiqueta: os papéis de venda e o de estoque (atendente de balcão), sem caixa aberto.
SCALE_ROLES = frozenset(SELL_ROLES | {"inventory"})
SETTINGS_KEY = "scale_label_settings"
SIZE_KEY = "scale_label_size:"
DEFAULT_SETTINGS = {"barcode_mode": "weight", "validity_days": 3, "manual_weight": "managers", "show_store_name": True}
FAVORITES_DAYS = 30
SEARCH_LIMIT = 60


def _settings(store, conn):
    raw = store._meta(conn, SETTINGS_KEY)
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    result = dict(DEFAULT_SETTINGS)
    if isinstance(saved, dict):
        if saved.get("barcode_mode") in BARCODE_MODES:
            result["barcode_mode"] = saved["barcode_mode"]
        if type(saved.get("validity_days")) is int and 0 <= saved["validity_days"] <= 365:
            result["validity_days"] = saved["validity_days"]
        if saved.get("manual_weight") in ("managers", "everyone"):
            result["manual_weight"] = saved["manual_weight"]
        if type(saved.get("show_store_name")) is bool:
            result["show_store_name"] = saved["show_store_name"]
    return result


def _station_size(store, conn):
    size = store._meta(conn, SIZE_KEY + store.current_station())
    return size if size in LABEL_SIZES else DEFAULT_SIZE


def _store_name(store, conn):
    raw = store._meta(conn, "issuer_profile")
    try:
        issuer = json.loads(raw) if raw else {}
    except ValueError:
        issuer = {}
    for value in (issuer.get("trade_name") if isinstance(issuer, dict) else None,
                  store._meta(conn, "shop_name"), store._meta(conn, "tenant_name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _product(conn, product_id):
    row = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (str(product_id or "")[:80],)).fetchone()
    if not row:
        raise TerminalError("Produto não encontrado", 404)
    product = dict(row)
    if product["unit"] != "KG":
        raise TerminalError("Somente produtos vendidos por quilo (KG) têm etiqueta de balança")
    override = conn.execute("SELECT validity_days FROM scale_product_settings WHERE product_id=?",
                            (product["id"],)).fetchone()
    product["validity_days"] = override["validity_days"] if override else None
    return product


def _public(product):
    return {key: product.get(key) for key in ("id", "name", "sku", "barcode", "scale_code", "unit", "category",
                                              "price_cents", "image_url", "validity_days")}


def settings(store):
    with store.connection() as conn:
        printer = store._print_setting(conn, "label")
        return {"settings": _settings(store, conn), "size": _station_size(store, conn),
                "sizes": list(LABEL_SIZES), "languages": [{"id": key, "name": LANGUAGE_NAMES[key]}
                                                         for key in LABEL_LANGUAGES],
                "printer": ({"driver": printer["driver"], "target": printer["target"], "format": printer["format"]}
                            if printer and printer["format"] in LABEL_LANGUAGES else None),
                "store_name": _store_name(store, conn)}


def save_settings(store, operator_id, data):
    with store.transaction() as conn:
        current = _settings(store, conn)
        changed = {}
        if "barcode_mode" in data:
            if data["barcode_mode"] not in BARCODE_MODES:
                raise TerminalError("Modo do código da etiqueta inválido")
            changed["barcode_mode"] = data["barcode_mode"]
        if "validity_days" in data:
            days = data["validity_days"]
            if type(days) is not int or not 0 <= days <= 365:
                raise TerminalError("Validade deve ficar entre 0 e 365 dias (0 = não imprimir)")
            changed["validity_days"] = days
        if "manual_weight" in data:
            if data["manual_weight"] not in ("managers", "everyone"):
                raise TerminalError("Permissão de peso manual inválida")
            changed["manual_weight"] = data["manual_weight"]
        if "show_store_name" in data:
            if type(data["show_store_name"]) is not bool:
                raise TerminalError("Opção de nome da loja inválida")
            changed["show_store_name"] = data["show_store_name"]
        if "size" in data:
            if data["size"] not in LABEL_SIZES:
                raise TerminalError("Tamanho de etiqueta inválido")
            store._set_meta(conn, SIZE_KEY + store.current_station(), data["size"])
        current.update(changed)
        store._set_meta(conn, SETTINGS_KEY, json.dumps(current, sort_keys=True))
        detail = "; ".join(f"{key}={value}" for key, value in sorted({**changed, **(
            {"size": data["size"]} if "size" in data else {})}.items()))
        store._audit(conn, operator_id, "scale.settings_changed", None, detail or None)
    return settings(store)


def set_product_validity(store, operator_id, product_id, days):
    if days is not None and (type(days) is not int or not 0 <= days <= 365):
        raise TerminalError("Validade deve ficar entre 0 e 365 dias")
    with store.transaction() as conn:
        product = _product(conn, product_id)
        if days is None:
            conn.execute("DELETE FROM scale_product_settings WHERE product_id=?", (product["id"],))
        else:
            conn.execute("INSERT INTO scale_product_settings VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET "
                         "validity_days=excluded.validity_days,updated_at=excluded.updated_at",
                         (product["id"], days, _now_ms()))
        store._audit(conn, operator_id, "scale.validity_changed", product["id"],
                     "padrão da loja" if days is None else f"{days} dias")
    return {"product_id": product["id"], "validity_days": days}


def search(store, query=""):
    """Produtos por quilo por nome, PLU, SKU ou código; sem busca, os mais etiquetados (favoritos)."""
    query = str(query or "")[:80].strip()
    with store.connection() as conn:
        if not query:
            since = _now_ms() - FAVORITES_DAYS * 86400000
            rows = conn.execute("""SELECT p.*, COUNT(l.id) AS uses FROM scale_labels l
                JOIN products p ON p.id=l.product_id
                WHERE l.created_at>=? AND p.active=1 AND p.unit='KG'
                GROUP BY p.id ORDER BY uses DESC, p.name LIMIT 12""", (since,)).fetchall()
            favorites = [_public(dict(row)) for row in rows]
            if len(favorites) < 12:
                seen = {row["id"] for row in favorites}
                for row in conn.execute("SELECT * FROM products WHERE active=1 AND unit='KG' "
                                        "ORDER BY name LIMIT 24"):
                    if row["id"] not in seen and len(favorites) < 12:
                        favorites.append(_public(dict(row)))
            total = conn.execute("SELECT COUNT(*) FROM products WHERE active=1 AND unit='KG'").fetchone()[0]
            return {"products": favorites, "favorites": True, "total": total}
        digits = query.lstrip("0") if query.isdigit() else None
        params = [query, query, query]
        exact_sql = "(barcode=? OR sku=? OR scale_code=?"
        if digits:
            # PLU digitado com ou sem zeros à esquerda: 42, 00042.
            exact_sql += " OR ltrim(scale_code,'0')=?"
            params.append(digits)
        exact_sql += ")"
        exact = conn.execute(f"SELECT * FROM products WHERE active=1 AND unit='KG' AND {exact_sql} "
                             "ORDER BY name LIMIT 5", params).fetchall()
        if exact:
            return {"products": [_public(dict(row)) for row in exact], "favorites": False, "total": len(exact)}
        label = decode_weighed(query)
        if label:
            # Uma etiqueta de balança lida aqui abre o produto dela.
            row = conn.execute("SELECT * FROM products WHERE active=1 AND unit='KG' AND scale_code IN (?,?) "
                               "ORDER BY name LIMIT 1", (label["scale_code"], label["scale_code"].lstrip("0"))).fetchone()
            if row:
                return {"products": [_public(dict(row))], "favorites": False, "total": 1}
        where, args = "active=1 AND unit='KG'", []
        for word in _fold(query).split():
            where += " AND search_text LIKE ? ESCAPE '\\'"
            args.append(f"%{_like(word)}%")
        total = conn.execute(f"SELECT COUNT(*) FROM products WHERE {where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM products WHERE {where} ORDER BY name LIMIT ?", (*args, SEARCH_LIMIT))
        return {"products": [_public(dict(row)) for row in rows], "favorites": False, "total": total}


def next_free_plu(store):
    """Menor PLU livre no catálogo local (sugestão ao gerente: o cadastro é feito no painel)."""
    with store.connection() as conn:
        used = set()
        for (code,) in conn.execute("SELECT scale_code FROM products WHERE scale_code IS NOT NULL AND scale_code!=''"):
            if str(code).isdigit():
                used.add(int(code))
    plu = next((number for number in range(1, 100000) if number not in used), None)
    if plu is None:
        raise TerminalError("Todos os PLUs de 1 a 99999 estão em uso")
    return {"plu": f"{plu:05d}"}


def print_label(store, operator, data, now=None):
    """Gera a etiqueta, registra a conferência e, havendo etiquetadora, põe na fila desta estação."""
    weight = data.get("weight_milli")
    if type(weight) is not int or weight <= 0:
        raise TerminalError("Peso inválido: coloque o produto na balança ou informe o peso")
    copies = data.get("copies", 1)
    if type(copies) is not int or not 1 <= copies <= MAX_COPIES:
        raise TerminalError(f"Cópias devem ficar entre 1 e {MAX_COPIES}")
    manual = data.get("manual", False)
    if type(manual) is not bool:
        raise TerminalError("Origem do peso inválida")
    output = data.get("output", "auto")
    if output not in ("auto", "browser"):
        raise TerminalError("Saída de impressão inválida")
    with store.connection() as conn:
        rules = _settings(store, conn)
    authorizer = None
    if manual and operator["role"] not in MANAGER_ROLES and rules["manual_weight"] != "everyone":
        # Sem balança: o peso digitado precisa de um gerente (o PIN vale para esta etiqueta).
        authorizer = store.manager_override(data.get("manager_pin"), operator["id"])
    with store.transaction() as conn:
        product = _product(conn, data.get("product_id"))
        validity = product["validity_days"] if product["validity_days"] is not None else rules["validity_days"]
        try:
            fields = label_fields(product, weight, rules["barcode_mode"], validity,
                                  _store_name(store, conn) if rules["show_store_name"] else "", now=now)
        except LabelError as exc:
            raise TerminalError(str(exc), 409, code="label_invalid") from exc
        # O caixa precisa achar ESTE produto pelo PLU da etiqueta (mesma busca de store.product_by_scale_code);
        # outro produto com o mesmo PLU confundiria a venda.
        resolved = conn.execute(
            "SELECT id FROM products WHERE active=1 AND unit='KG' AND scale_code IN (?,?) ORDER BY name LIMIT 1",
            (fields["plu"], fields["plu"].lstrip("0") or fields["plu"])).fetchone()
        if not resolved or resolved["id"] != product["id"]:
            raise TerminalError("O PLU deste produto está repetido ou não é lido pelo caixa. "
                                "Corrija o código de balança no painel, em Produtos.", 409, code="label_invalid")
        size = _station_size(store, conn)
        printer = store._print_setting(conn, "label")
        job_id = None
        if output == "auto" and printer and printer["format"] in LABEL_LANGUAGES:
            payload = weighed_label_bytes(fields, printer["format"], size, copies)
            job_id = store._queue_print(conn, "label", payload, printer)
        where = "printer" if job_id else "browser"
        label_id, created = uuid.uuid4().hex, _now_ms()
        packed = int((now or datetime.now()).timestamp() * 1000)
        conn.execute("""INSERT INTO scale_labels(id,product_id,product_name,scale_code,weight_milli,price_cents,
            total_cents,barcode,barcode_mode,copies,manual,authorizer_id,operator_id,station_id,output,job_id,
            packed_at,valid_until,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (label_id, product["id"], product["name"], fields["plu"], weight, product["price_cents"],
                      fields["total_cents"], fields["barcode"], rules["barcode_mode"], copies, int(manual),
                      authorizer, operator["id"], store.current_station(), where, job_id, packed,
                      fields["valid_until"], created))
        detail = (f"peso={weight}g; valor={fields['total_cents']}; codigo={fields['barcode']}; copias={copies}; "
                  f"saida={where}" + ("; peso digitado" if manual else "")
                  + (f"; autorizado_por={authorizer}" if authorizer else ""))
        store._audit(conn, operator["id"], "scale.label_printed", product["id"], detail)
    return {"id": label_id, "label": fields, "size": size, "copies": copies, "output": where,
            "job": {"id": job_id, "status": "queued"} if job_id else None,
            "printer": printer["format"] if job_id else None}


def recent_labels(store, limit=50):
    with store.connection() as conn:
        rows = conn.execute("""SELECT l.*, o.name AS operator_name FROM scale_labels l
            LEFT JOIN operators o ON o.id=l.operator_id ORDER BY l.created_at DESC LIMIT ?""", (limit,))
        return [dict(row) for row in rows]


def register_routes(app, store, authenticated, body):
    """Rotas /api/scale/* (o app passa o próprio decorador de sessão e o leitor de JSON)."""

    @app.get("/api/scale/settings")
    @authenticated(*SCALE_ROLES)
    def scale_settings(operator):
        result = settings(store)
        manager = operator["role"] in MANAGER_ROLES
        result["permissions"] = {"manage": manager,
                                 "manual_weight": manager or result["settings"]["manual_weight"] == "everyone"}
        return jsonify(result)

    @app.post("/api/scale/settings")
    @authenticated(*MANAGER_ROLES)
    def scale_settings_save(operator):
        return jsonify(save_settings(store, operator["id"], body()))

    @app.get("/api/scale/products")
    @authenticated(*SCALE_ROLES)
    def scale_products(_operator):
        return jsonify(search(store, request.args.get("q", "")))

    @app.get("/api/scale/next-plu")
    @authenticated(*MANAGER_ROLES)
    def scale_next_plu(_operator):
        return jsonify(next_free_plu(store))

    @app.post("/api/scale/products/<product_id>/validity")
    @authenticated(*MANAGER_ROLES)
    def scale_product_validity(operator, product_id):
        return jsonify(set_product_validity(store, operator["id"], product_id, body().get("validity_days")))

    @app.post("/api/scale/labels")
    @authenticated(*SCALE_ROLES)
    def scale_print(operator):
        return jsonify(print_label(store, operator, body()))

    @app.get("/api/scale/labels")
    @authenticated(*MANAGER_ROLES)
    def scale_labels(_operator):
        return jsonify({"labels": recent_labels(store)})
