"""Wspólne funkcje pomocnicze widoków."""
from __future__ import annotations

import os
import re
from datetime import date, datetime

from flask import current_app

from .. import fx
from ..models import Attachment, Bill, Category, Person, Product, Trip, TripRate, db
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


def rate_label(trip: Trip, currency: str) -> str:
    """'1 EUR = 4,3895 zł (NBP, 10.11.2026)'; pusty, gdy brak kursu."""
    tr = trip.rate_for(currency)
    if tr is None or currency == trip.currency:
        return ""
    if tr.source == "manual":
        meta = "kurs ręczny"
    else:
        meta = ", ".join(x for x in (tr.source, tr.rate_date.strftime("%d.%m.%Y") if tr.rate_date else "") if x)
    return f"1 {currency} = {fmt_rate(tr.value)} {currency_symbol(trip.currency)}" + (f" ({meta})" if meta else "")


def _fetch(trip: Trip, currency: str, day: date | None) -> fx.Rate | str:
    try:
        return fx.get_rate(currency, trip.currency, day)
    except fx.RateError as exc:
        return str(exc)


def ensure_rate(trip: Trip, currency: str | None, day: date | None = None) -> list[tuple[str, str]]:
    """Gdy waluta pierwszy raz pojawia się w wyjeździe - pobiera jej kurs z NBP (z dnia `day`).

    Kurs pobieramy tylko raz; potem zmienia się wyłącznie ręcznie albo przyciskiem „odśwież”.
    """
    if not currency or currency == trip.currency or trip.rate_for(currency) is not None:
        return []
    got = _fetch(trip, currency, day)
    if isinstance(got, str):
        return [("danger", f"{got} Rachunki w {currency} nie wejdą do rozliczenia, dopóki nie wpiszesz kursu "
                           f"(Rozliczenie → Kursy walut).")]
    tr = TripRate(currency=currency)
    tr.set(got.value, got.day, got.source)
    trip.rates.append(tr)
    return [("info", f"Nowa waluta w wyjeździe: {currency}. Kurs pobrany raz i używany dla wszystkich rachunków "
                     f"w {currency}: {rate_label(trip, currency)}. Zmienisz go w Rozliczenie → Kursy walut.")]


def refresh_rate(trip: Trip, currency: str) -> list[tuple[str, str]]:
    """Pobiera najnowszy kurs NBP (nadpisuje też kurs ręczny)."""
    if currency == trip.currency:
        return []
    got = _fetch(trip, currency, None)
    if isinstance(got, str):
        return [("danger", got)]
    tr = trip.rate_for(currency)
    if tr is None:
        tr = TripRate(currency=currency)
        trip.rates.append(tr)
    tr.set(got.value, got.day, got.source)
    return [("success", f"Odświeżono kurs: {rate_label(trip, currency)}.")]


def set_manual_rate(trip: Trip, currency: str, value) -> bool:
    """Zapisuje kurs wpisany ręcznie; zwraca True, gdy coś się zmieniło."""
    tr = trip.rate_for(currency)
    if tr is not None and tr.value == value:
        return False
    if tr is None:
        tr = TripRate(currency=currency)
        trip.rates.append(tr)
    tr.set(value, None, "manual")
    return True


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
    if data.get("date"):
        bill.date = data["date"]
    detected = normalize_currency(data.get("currency"))
    bill.currency = currency or detected or bill.currency or bill.trip_currency
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
    if bill.trip is not None:
        messages += ensure_rate(bill.trip, bill.currency, bill.date)
    return messages
