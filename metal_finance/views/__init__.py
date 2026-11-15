"""Wspólne funkcje pomocnicze widoków."""
from __future__ import annotations

import os
import re
from datetime import date, datetime

from flask import current_app

from .. import fx
from ..models import Attachment, Bill, Category, Person, Product, db
from ..money import currency_symbol, fmt_rate, normalize_currency


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


def form_currency(value, default: str | None) -> str | None:
    """Waluta z formularza: kod ISO albo `default`, gdy pole puste / niepoprawne."""
    return normalize_currency(value) or default


def rate_label(bill: Bill) -> str:
    """'1 EUR = 4,3895 zł (NBP, 10.11.2026)'."""
    if not bill.is_foreign or bill.rate_value is None:
        return ""
    src = "kurs ręczny" if bill.rate_source == "manual" else (bill.rate_source or "")
    when = bill.rate_date.strftime("%d.%m.%Y") if bill.rate_date and bill.rate_source != "manual" else ""
    meta = ", ".join(x for x in (src, when) if x)
    return f"1 {bill.currency} = {fmt_rate(bill.rate_value)} {currency_symbol(bill.trip_currency)}" + (f" ({meta})" if meta else "")


def update_rate(bill: Bill, force: bool = False) -> list[tuple[str, str]]:
    """Pobiera kurs waluty rachunku na walutę wyjazdu z dnia rachunku (NBP, zapasowo EBC).

    Kurs wpisany ręcznie zostaje, chyba że force=True. Gdy pobranie się nie uda, zostaje dotychczasowy kurs.
    """
    if not bill.is_foreign:
        bill.set_rate(None)
        return []
    if bill.rate_source == "manual" and bill.exchange_rate and not force:
        return []
    try:
        rate = fx.get_rate(bill.currency, bill.trip_currency, bill.date)
    except fx.RateError as exc:
        if bill.exchange_rate:
            return [("warning", f"{exc} Zostaje dotychczasowy kurs: {rate_label(bill)}.")]
        return [("danger", f"{exc} Rachunek nie wejdzie do rozliczenia, dopóki nie wpiszesz kursu ręcznie (Dane rachunku → Kurs).")]
    bill.set_rate(rate.value, rate.day, rate.source)
    return [("info", f"Rachunek w {bill.currency} - przeliczany na {bill.trip_currency} po kursie {rate_label(bill)}.")]


def apply_receipt(bill: Bill, data: dict, consumers: list[Person], replace: bool = True,
                  currency: str | None = None) -> list[tuple[str, str]]:
    """Wpisuje wynik odczytu paragonu do rachunku. Zwraca komunikaty (kategoria, tekst) do flash().

    `currency` - waluta wybrana przez użytkownika; None = ta, którą rozpoznał Claude.
    """
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
    old_currency, old_date = bill.currency, bill.date
    if data.get("date"):
        bill.date = data["date"]
    detected = normalize_currency(data.get("currency"))
    new_currency = currency or detected or bill.currency or bill.trip_currency
    if new_currency != old_currency:
        bill.currency = new_currency
        bill.set_rate(None)
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
    if currency and detected and detected != currency:
        messages.append(("warning", f"Claude rozpoznał walutę {detected}, ale zostaje wybrana przez Ciebie {currency}."))
    if bill.is_foreign and (bill.missing_rate or bill.currency != old_currency or bill.date != old_date):
        messages += update_rate(bill)
    elif not bill.is_foreign:
        bill.set_rate(None)
    return messages
