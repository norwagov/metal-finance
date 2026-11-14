"""Wspólne funkcje pomocnicze widoków."""
from __future__ import annotations

import os
import re
from datetime import date, datetime

from flask import current_app

from ..models import Attachment, Bill, Category, Person, Product, db


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def clean(value: str | None, limit: int = 200) -> str:
    return " ".join((value or "").split())[:limit]


def int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def selected_people(form, field: str, allowed: list[Person]) -> list[Person]:
    """Osoby zaznaczone w formularzu, ograniczone do dozwolonej listy (uczestników wyjazdu)."""
    ids = {int_or_none(v) for v in form.getlist(field)}
    return [p for p in allowed if p.id in ids]


def find_or_create_people(raw: str) -> list[Person]:
    """'Ania, Bartek; Czarek' -> lista osób (istniejących lub nowych), bez duplikatów."""
    existing = {p.name.casefold(): p for p in Person.query.all()}
    out: list[Person] = []
    for part in re.split(r"[,;\n]", raw or ""):
        name = clean(part, 80)
        if not name:
            continue
        person = existing.get(name.casefold())
        if person is None:
            person = Person(name=name)
            db.session.add(person)
            db.session.flush()
            existing[name.casefold()] = person
        if person not in out:
            out.append(person)
    return out


def match_category(name: str | None) -> Category | None:
    if not name:
        return None
    key = name.strip().casefold()
    for c in Category.query.all():
        if c.name.casefold() == key:
            return c
    return None


def attachment_path(att: Attachment) -> str:
    return os.path.join(current_app.config["UPLOAD_DIR"], att.filename)


def remove_files(attachments) -> None:
    for att in attachments:
        try:
            os.remove(attachment_path(att))
        except OSError:
            pass


def apply_receipt(bill: Bill, data: dict, consumers: list[Person], replace: bool = True) -> list[tuple[str, str]]:
    """Wpisuje wynik odczytu paragonu do rachunku. Zwraca komunikaty (kategoria, tekst) do flash()."""
    messages: list[tuple[str, str]] = []
    if replace:
        bill.products.clear()
    start = len(bill.products)
    for i, item in enumerate(data["items"]):
        bill.products.append(
            Product(
                name=item["name"],
                quantity=item["quantity"],
                price=item["price"],
                position=start + i,
                consumers=list(consumers),
            )
        )
    if data.get("merchant") and not bill.title.strip():
        bill.title = data["merchant"]
    if not bill.title.strip():
        bill.title = "Paragon"
    if data.get("date"):
        bill.date = data["date"]
    if bill.category is None:
        cat = match_category(data.get("category"))
        if cat:
            bill.category = cat
    bill.declared_total = data.get("total")
    bill.source = "ai"
    if data.get("notes") and not (bill.note or "").strip():
        bill.note = f"Claude: {data['notes']}"

    n = len(data["items"])
    if n:
        messages.append(("success", f"Claude odczytał {n} poz. z paragonu „{bill.title}”. Sprawdź je i zaznacz, kto z czego korzystał."))
    else:
        messages.append(("warning", "Claude nie znalazł na zdjęciu żadnych pozycji."))
    if data.get("notes"):
        messages.append(("info", f"Uwagi Claude: {data['notes']}"))
    trip_cur = bill.trip.currency if bill.trip else None
    if data.get("currency") and trip_cur and data["currency"] != trip_cur:
        messages.append(
            ("warning", f"Paragon jest w walucie {data['currency']}, a wyjazd w {trip_cur}. Kwoty nie zostały przeliczone.")
        )
    return messages
