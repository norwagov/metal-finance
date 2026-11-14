"""Małe API JSON dla edycji „jak w Excelu” (tabela wyjazdu i strona rachunku)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..models import Product, db
from ..money import fmt_amount, fmt_input, fmt_qty, parse_money, parse_quantity
from ..settlement import bill_shares, product_shares, summarize
from . import clean

bp = Blueprint("api", __name__, url_prefix="/api")


WEIGHTS_LOCKED_MSG = "Wagi zmieniają podział - ustaw wszystkim tę samą wagę (np. 1), żeby je wyłączyć."


def _tone(cents: int) -> str:
    return "pos" if cents > 0 else "neg" if cents < 0 else "zero"


def _state(prod: Product) -> dict:
    """Wszystkie liczby, które mogły się zmienić po edycji pozycji - JS podmienia je w miejscu."""
    bill = prod.bill
    trip = bill.trip
    cur = trip.currency
    summary = summarize(trip)
    bind: dict[str, str] = {}
    tone: dict[str, str] = {}

    shares = product_shares(prod)
    for p in trip.participants:
        bind[f"share:{prod.id}:{p.id}"] = fmt_amount(shares[p.id]) if p.id in shares else ""
    bind[f"price:{prod.id}"] = fmt_amount(prod.price, cur)

    bshares = bill_shares(bill)
    bind[f"bill-total:{bill.id}"] = fmt_amount(bill.total, cur)
    for p in trip.participants:
        bind[f"bill-share:{bill.id}:{p.id}"] = fmt_amount(bshares[p.id], cur) if p.id in bshares else "—"
    mismatch = bill.total_mismatch
    bind[f"bill-mismatch:{bill.id}"] = (
        f"Suma pozycji różni się od paragonu o {fmt_amount(mismatch, cur, sign=True)}" if mismatch else ""
    )

    for r in summary.people:
        pid = r.person.id
        bind[f"paid:{pid}"] = fmt_amount(r.paid, cur)
        bind[f"consumed:{pid}"] = fmt_amount(r.consumed_cents, cur)
        bind[f"balance:{pid}"] = fmt_amount(r.balance, cur, sign=True)
        tone[f"balance:{pid}"] = _tone(r.balance)
    bind["trip-total"] = fmt_amount(summary.total, cur)
    bind["unassigned-count"] = str(len(summary.unassigned))
    bind[f"bill-unassigned:{bill.id}"] = str(bill.unassigned_count)

    return {
        "ok": True,
        "bind": bind,
        "tone": tone,
        "rows": {str(prod.id): {
            "consumers": [c.id for c in prod.consumers],
            "unassigned": not prod.consumers,
            "weighted": prod.weighted,
            "locked": prod.weights_locked,
            "weights": {str(p.id): fmt_qty(prod.weight_of(p.id)) for p in trip.participants},
        }},
        "values": {"name": prod.name, "quantity": fmt_qty(prod.quantity), "price": fmt_input(prod.price)},
        "flags": {"unassigned_any": bool(summary.unassigned), f"bill-unassigned:{bill.id}": bill.unassigned_count > 0},
    }


def _error(msg: str, status: int = 400):
    return jsonify({"ok": False, "error": msg}), status


@bp.post("/products/<int:product_id>/consumers")
def consumers(product_id: int):
    prod = db.get_or_404(Product, product_id)
    data = request.get_json(silent=True) or {}
    participants = prod.bill.trip.participants
    mode = data.get("set")
    if mode == "all":
        prod.consumers = list(participants)
    elif mode == "none":
        prod.consumers = []
    else:
        person = next((p for p in participants if p.id == data.get("person_id")), None)
        if person is None:
            return _error("Ta osoba nie jest uczestnikiem wyjazdu.")
        on = bool(data.get("on"))
        if on and person not in prod.consumers:
            prod.consumers.append(person)
        elif not on and person in prod.consumers:
            prod.consumers.remove(person)
    db.session.commit()
    return jsonify(_state(prod))


@bp.post("/products/<int:product_id>")
def update_product(product_id: int):
    prod = db.get_or_404(Product, product_id)
    data = request.get_json(silent=True) or {}
    try:
        if "name" in data:
            prod.name = clean(str(data["name"])) or prod.name
        if "quantity" in data:
            prod.quantity = parse_quantity(data["quantity"])
        if "weighted" in data:
            if not data["weighted"] and prod.weights_locked:
                return _error(WEIGHTS_LOCKED_MSG)
            prod.weighted = bool(data["weighted"])
        if "price" in data:
            price = parse_money(data["price"])
            if price is None:
                return _error("Podaj kwotę.")
            prod.price = price
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc))
    db.session.commit()
    return jsonify(_state(prod))


MAX_WEIGHT = 1000


@bp.post("/products/<int:product_id>/weights")
def set_weight(product_id: int):
    prod = db.get_or_404(Product, product_id)
    data = request.get_json(silent=True) or {}
    person = next((p for p in prod.bill.trip.participants if p.id == data.get("person_id")), None)
    if person is None:
        return _error("Ta osoba nie jest uczestnikiem wyjazdu.")
    try:
        weight = parse_quantity(data.get("weight"))
    except ValueError:
        return _error("Waga musi być liczbą, np. 1 albo 1,5.")
    if not 0 <= weight <= MAX_WEIGHT:
        return _error(f"Waga musi być w zakresie 0-{MAX_WEIGHT}.")
    prod.set_weight(person.id, weight)
    db.session.commit()
    return jsonify(_state(prod))
