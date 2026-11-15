from __future__ import annotations

import csv
import io
from datetime import date
from fractions import Fraction

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from .. import receipt_ai
from ..models import Payment, Person, Trip, db
from ..money import (
    CURRENCIES, fmt_amount, fmt_input, fmt_qty, fmt_rate, normalize_currency, parse_money, parse_rate, round_cents,
)
from ..settlement import bill_shares, product_shares, summarize
from . import (
    clean, ensure_rate, find_or_create_people, int_or_none, parse_date, rate_label, refresh_rate, remove_files,
    set_manual_rate,
)

bp = Blueprint("trips", __name__)


@bp.get("/")
def index():
    trips = Trip.query.order_by(Trip.start_date.desc().nulls_last(), Trip.created_at.desc()).all()
    return render_template("index.html", trips=trips)


def _trip_form_people() -> list[Person]:
    people = Person.query.filter(Person.id.in_(
        [i for i in (int_or_none(v) for v in request.form.getlist("participants")) if i]
    )).all() if request.form.getlist("participants") else []
    for p in find_or_create_people(request.form.get("new_people", "")):
        if p not in people:
            people.append(p)
    return sorted(people, key=lambda p: p.name.casefold())


def _change_currency(trip: Trip, old: str) -> None:
    """Po zmianie waluty wyjazdu: rachunki w starej walucie wyjazdu przechodzą na nową (jak dotąd -
    to zwykle poprawka pomyłki), a kursy walut pobieramy na nowo, bo były liczone wobec starej."""
    for b in trip.bills:
        if b.currency == old:
            b.currency = trip.currency
    trip.rates.clear()
    db.session.flush()
    failed = []
    for cur in trip.foreign_currencies:
        first = min((b.date or date.today() for b in trip.bills if b.currency == cur), default=None)
        if any(level != "info" for level, _ in ensure_rate(trip, cur, first)):
            failed.append(cur)
    if failed:
        flash(f"Nie udało się pobrać kursu: {', '.join(failed)} - wpisz go ręcznie w zakładce Waluty.", "warning")


def _fill_trip(trip: Trip) -> str | None:
    name = clean(request.form.get("name"), 120)
    if not name:
        return "Podaj nazwę wyjazdu."
    trip.name = name
    trip.description = (request.form.get("description") or "").strip()
    trip.currency = normalize_currency(request.form.get("currency")) or trip.currency or "PLN"
    trip.start_date = parse_date(request.form.get("start_date"))
    trip.end_date = parse_date(request.form.get("end_date"))
    return None


@bp.route("/trips/new", methods=["GET", "POST"])
def new():
    trip = Trip(currency="PLN")
    if request.method == "POST":
        error = _fill_trip(trip)
        people = _trip_form_people()
        if not error and not people:
            error = "Dodaj przynajmniej jednego uczestnika."
        if error:
            db.session.rollback()
            flash(error, "danger")
            return render_template("trip_form.html", trip=trip, people=Person.query.order_by(Person.name).all(),
                                   chosen={int_or_none(v) for v in request.form.getlist("participants")},
                                   currencies=CURRENCIES, form=request.form)
        trip.participants = people
        db.session.add(trip)
        db.session.commit()
        flash(f"Utworzono wyjazd „{trip.name}”. Dodaj pierwszy rachunek!", "success")
        return redirect(url_for("trips.show", trip_id=trip.id))
    return render_template("trip_form.html", trip=trip, people=Person.query.order_by(Person.name).all(),
                           chosen=set(), currencies=CURRENCIES, form={})


@bp.route("/trips/<int:trip_id>/edit", methods=["GET", "POST"])
def edit(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    if request.method == "POST":
        old_currency = trip.currency
        error = _fill_trip(trip)
        people = _trip_form_people()
        if error or not people:
            db.session.rollback()
            flash(error or "Wyjazd musi mieć przynajmniej jednego uczestnika.", "danger")
            return redirect(url_for("trips.edit", trip_id=trip.id))
        new_ids = {p.id for p in people}
        kept = []
        for p in trip.participants:
            if p.id in new_ids:
                continue
            busy = any(b.payer_id == p.id for b in trip.bills) or any(
                p.id in (pay.from_id, pay.to_id) for pay in trip.payments
            )
            if busy:
                kept.append(p)
                flash(f"{p.name} płacił(a) za rachunek lub ma spłatę w tym wyjeździe - nie można go/jej usunąć.", "warning")
            else:
                removed = 0
                for b in trip.bills:
                    for prod in b.products:
                        if p in prod.consumers:
                            prod.consumers.remove(p)
                            removed += 1
                if removed:
                    flash(f"Usunięto {p.name} z {removed} poz.", "info")
        trip.participants = sorted(people + kept, key=lambda x: x.name.casefold())
        if trip.currency != old_currency:
            _change_currency(trip, old_currency)
        db.session.commit()
        flash("Zapisano zmiany.", "success")
        return redirect(url_for("trips.show", trip_id=trip.id))
    return render_template("trip_form.html", trip=trip, people=Person.query.order_by(Person.name).all(),
                           chosen={p.id for p in trip.participants}, currencies=CURRENCIES, form={})


@bp.post("/trips/<int:trip_id>/delete")
def delete(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    for b in trip.bills:
        remove_files(b.attachments)
    db.session.delete(trip)
    db.session.commit()
    flash(f"Usunięto wyjazd „{trip.name}”.", "success")
    return redirect(url_for("trips.index"))


@bp.get("/trips/<int:trip_id>")
def show(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    cat_filter = request.args.get("category", "")
    bills = trip.bills
    if cat_filter == "none":
        bills = [b for b in bills if b.category is None]
    elif cat_filter.isdigit():
        bills = [b for b in bills if b.category_id == int(cat_filter)]
    used_categories = sorted({b.category for b in trip.bills if b.category}, key=lambda c: c.name)
    return render_template(
        "trip.html", trip=trip, bills=bills, summary=summarize(trip), tab="bills",
        used_categories=used_categories, cat_filter=cat_filter, has_uncategorized=any(b.category is None for b in trip.bills),
        claude_ready=receipt_ai.is_configured(),
    )


@bp.get("/trips/<int:trip_id>/table")
def table(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    bills = sorted(trip.bills, key=lambda b: (b.date or date.min, b.id))
    return render_template("trip_table.html", trip=trip, bills=bills, summary=summarize(trip),
                           tab="table", product_shares=product_shares, bill_shares=bill_shares)


@bp.get("/trips/<int:trip_id>/settle")
def settle(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    summary = summarize(trip)
    sym = trip.currency
    lines = [f"💸 Rozliczenie: {trip.name}", f"Wydatki razem: {fmt_amount(summary.total, sym)}", ""]
    if summary.transfers:
        lines += [f"• {t.sender.name} → {t.receiver.name}: {fmt_amount(t.amount, sym)}" for t in summary.transfers]
    else:
        lines.append("Wszyscy są kwita 🤘")
    return render_template("trip_settle.html", trip=trip, summary=summary, tab="settle",
                           share_text="\n".join(lines), today=date.today())


@bp.post("/trips/<int:trip_id>/payments")
def add_payment(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    sender = next((p for p in trip.participants if p.id == int_or_none(request.form.get("from_id"))), None)
    receiver = next((p for p in trip.participants if p.id == int_or_none(request.form.get("to_id"))), None)
    try:
        amount = parse_money(request.form.get("amount"))
    except ValueError:
        amount = None
    if not sender or not receiver or sender.id == receiver.id or not amount or amount <= 0:
        flash("Podaj poprawną spłatę: dwie różne osoby i kwotę większą od zera.", "danger")
        return redirect(url_for("trips.settle", trip_id=trip.id))
    db.session.add(Payment(trip=trip, sender=sender, receiver=receiver, amount=amount,
                           date=parse_date(request.form.get("date")) or date.today(),
                           note=clean(request.form.get("note"), 200)))
    db.session.commit()
    flash(f"Zapisano spłatę: {sender.name} → {receiver.name} {fmt_amount(amount, trip.currency)}.", "success")
    return redirect(url_for("trips.settle", trip_id=trip.id))


@bp.get("/trips/<int:trip_id>/rates")
def rates(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    rows = []
    for cur, tr in trip.rate_rows:
        bills = [b for b in trip.bills if b.currency == cur]
        total = sum(b.total for b in bills)
        rows.append({"currency": cur, "rate": tr, "bills": bills, "total": total,
                     "converted": total * Fraction(tr.value) if tr else None})
    return render_template("trip_rates.html", trip=trip, summary=summarize(trip), tab="rates", rows=rows)


@bp.post("/trips/<int:trip_id>/rates")
def save_rates(trip_id: int):
    """Ręczne kursy walut wyjazdu (pola rate_<WALUTA>)."""
    trip = db.get_or_404(Trip, trip_id)
    changed, errors = [], []
    for cur in trip.foreign_currencies:
        raw = request.form.get(f"rate_{cur}")
        try:
            value = parse_rate(raw)
        except ValueError:
            errors.append(f"{cur}: niepoprawny kurs „{raw}”")
            continue
        if value is not None and set_manual_rate(trip, cur, value):
            changed.append(rate_label(trip, cur))
    db.session.commit()
    for e in errors:
        flash(e, "danger")
    if changed:
        flash("Zapisano kurs: " + "; ".join(changed) + ".", "success")
    elif not errors:
        flash("Kursy bez zmian.", "info")
    return redirect(url_for("trips.rates", trip_id=trip.id))


@bp.post("/trips/<int:trip_id>/rates/<currency>/refresh")
def refresh_rate_view(trip_id: int, currency: str):
    """Pobiera najnowszy kurs NBP dla waluty (nadpisuje kurs ręczny)."""
    trip = db.get_or_404(Trip, trip_id)
    cur = normalize_currency(currency)
    if cur not in trip.foreign_currencies:
        flash("Ta waluta nie występuje w rachunkach wyjazdu.", "warning")
    else:
        for level, msg in refresh_rate(trip, cur):
            flash(msg, level)
        db.session.commit()
    nxt = request.form.get("next", "")
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = url_for("trips.rates", trip_id=trip.id)
    return redirect(nxt)


@bp.post("/payments/<int:payment_id>/delete")
def delete_payment(payment_id: int):
    pay = db.get_or_404(Payment, payment_id)
    trip_id = pay.trip_id
    db.session.delete(pay)
    db.session.commit()
    flash("Usunięto spłatę.", "success")
    return redirect(url_for("trips.settle", trip_id=trip_id))


@bp.get("/trips/<int:trip_id>/export.csv")
def export_csv(trip_id: int):
    """CSV pod polskiego Excela: średniki, przecinek dziesiętny, BOM UTF-8."""
    trip = db.get_or_404(Trip, trip_id)
    people = trip.participants
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    cur = trip.currency
    head = ["Data", "Rachunek", "Kategoria", "Płacił", "Pozycja", "Ilość", "Kwota", "Waluta", "Kurs", f"Kwota {cur}"]
    w.writerow(head + [f"{p.name} ({cur})" for p in people])
    pad = [""] * (len(head) - 5)
    for bill in sorted(trip.bills, key=lambda b: (b.date or date.min, b.id)):
        rate = bill.rate
        for prod in bill.products:
            shares = product_shares(prod)
            w.writerow([
                bill.date.isoformat() if bill.date else "", bill.title,
                bill.category.name if bill.category else "", bill.payer.name, prod.name,
                fmt_qty(prod.quantity), fmt_input(prod.price), bill.currency,
                fmt_rate(bill.rate_value) if rate is not None else "brak",
                fmt_input(round_cents(prod.price * rate)) if rate is not None else "",
            ] + [fmt_input(round_cents(shares[p.id] * rate)) if p.id in shares and rate is not None else "" for p in people])
    summary = summarize(trip)
    w.writerow([])
    w.writerow(["", "", "", "", "Zapłacił", *pad] + [fmt_input(summary.row(p.id).paid) for p in people])
    w.writerow(["", "", "", "", "Skonsumował", *pad] + [fmt_input(summary.row(p.id).consumed_cents) for p in people])
    w.writerow(["", "", "", "", "Saldo", *pad] + [fmt_input(summary.row(p.id).balance) for p in people])
    filename = f"metal-finance-{trip.id}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
