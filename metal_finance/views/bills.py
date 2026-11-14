from __future__ import annotations

import re
from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for

from .. import receipt_ai
from ..images import save_image
from ..models import Attachment, Bill, Category, Product, Trip, db
from ..money import fmt_amount, parse_money, parse_quantity
from ..settlement import bill_shares, product_shares, summarize
from . import apply_receipt, attachment_path, clean, int_or_none, parse_date, remove_files, selected_people

bp = Blueprint("bills", __name__)

_UPLOAD_RE = re.compile(r"^[0-9a-f]{32}\.jpg$")


def _categories():
    return Category.query.order_by(Category.name).all()


def _save_photos(bill: Bill) -> None:
    for fs in request.files.getlist("photos"):
        if not fs or not fs.filename:
            continue
        name = save_image(fs, current_app.config["UPLOAD_DIR"])  # ValueError gdy to nie obraz
        bill.attachments.append(Attachment(filename=name))


def _run_claude(bill: Bill, consumers, replace: bool = True) -> bool:
    paths = [attachment_path(a) for a in bill.attachments]
    try:
        data = receipt_ai.parse_receipt(paths, [c.name for c in _categories()], bill.trip.currency)
    except receipt_ai.ReceiptError as exc:
        flash(f"Nie udało się odczytać paragonu: {exc}", "danger")
        return False
    for level, msg in apply_receipt(bill, data, consumers, replace=replace):
        flash(msg, level)
    return True


def _default_payer(trip: Trip):
    if trip.bills:
        return trip.bills[0].payer_id
    return trip.participants[0].id if trip.participants else None


@bp.route("/trips/<int:trip_id>/bills/new", methods=["GET", "POST"])
def new(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    mode = request.values.get("mode", "manual")
    mode = mode if mode in ("manual", "ai") else "manual"
    if not trip.participants:
        flash("Najpierw dodaj uczestników wyjazdu.", "warning")
        return redirect(url_for("trips.edit", trip_id=trip.id))

    def render(form):
        return render_template(
            "bill_new.html", trip=trip, mode=mode, categories=_categories(), form=form,
            default_payer=_default_payer(trip), today=date.today(), claude_ready=receipt_ai.is_configured(),
        )

    if request.method == "GET":
        return render({})

    form = request.form
    payer = next((p for p in trip.participants if p.id == int_or_none(form.get("payer_id"))), None)
    if payer is None:
        flash("Wybierz, kto płacił.", "danger")
        return render(form)
    consumers = selected_people(form, "consumers", trip.participants)
    cat = db.session.get(Category, int_or_none(form.get("category_id"))) if form.get("category_id") else None
    amount = None
    if mode == "manual":
        try:
            amount = parse_money(form.get("amount"))
        except ValueError:
            flash("Niepoprawna kwota.", "danger")
            return render(form)

    bill = Bill(
        trip=trip, payer=payer, category=cat, title=clean(form.get("title")),
        date=parse_date(form.get("date")) or date.today(), note=(form.get("note") or "").strip(),
    )
    try:
        _save_photos(bill)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return render(form)

    if mode == "ai":
        if not bill.attachments:
            db.session.rollback()
            flash("Dodaj zdjęcie paragonu.", "danger")
            return render(form)
        db.session.add(bill)
        db.session.flush()
        ok = _run_claude(bill, consumers)
        if not bill.title:
            bill.title = "Paragon"
        db.session.commit()
        if not ok:
            flash("Rachunek zapisano razem ze zdjęciem - możesz ponowić odczyt albo dodać pozycje ręcznie.", "info")
        return redirect(url_for("bills.show", bill_id=bill.id))

    if not bill.title:
        bill.title = cat.name if cat else "Rachunek"
    if amount is not None:
        bill.products.append(Product(name=bill.title, quantity=1, price=amount, consumers=consumers))
    db.session.add(bill)
    db.session.commit()
    if amount is not None and form.get("next") != "items":
        flash(f"Dodano „{bill.title}” - {fmt_amount(amount, trip.currency)} (płacił(a) {payer.name}).", "success")
        return redirect(url_for("trips.show", trip_id=trip.id))
    flash("Rachunek utworzony - dodaj pozycje.", "success")
    return redirect(url_for("bills.show", bill_id=bill.id))


@bp.get("/bills/<int:bill_id>")
def show(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    return render_template(
        "bill.html", bill=bill, trip=bill.trip, categories=_categories(), summary=summarize(bill.trip),
        shares=bill_shares(bill), product_shares=product_shares, claude_ready=receipt_ai.is_configured(),
    )


@bp.post("/bills/<int:bill_id>/edit")
def edit(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    form = request.form
    payer = next((p for p in bill.trip.participants if p.id == int_or_none(form.get("payer_id"))), None)
    if payer is None:
        flash("Wybierz, kto płacił.", "danger")
        return redirect(url_for("bills.show", bill_id=bill.id))
    try:
        declared = parse_money(form.get("declared_total"))
    except ValueError:
        flash("Niepoprawna kwota z paragonu.", "danger")
        return redirect(url_for("bills.show", bill_id=bill.id))
    bill.payer = payer
    bill.title = clean(form.get("title")) or bill.title
    bill.category = db.session.get(Category, int_or_none(form.get("category_id"))) if form.get("category_id") else None
    bill.date = parse_date(form.get("date")) or bill.date
    bill.note = (form.get("note") or "").strip()
    bill.declared_total = declared
    db.session.commit()
    flash("Zapisano rachunek.", "success")
    return redirect(url_for("bills.show", bill_id=bill.id))


@bp.post("/bills/<int:bill_id>/delete")
def delete(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    trip_id = bill.trip_id
    remove_files(bill.attachments)
    db.session.delete(bill)
    db.session.commit()
    flash(f"Usunięto rachunek „{bill.title}”.", "success")
    return redirect(url_for("trips.show", trip_id=trip_id))


@bp.post("/bills/<int:bill_id>/products")
def add_product(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    form = request.form
    try:
        price = parse_money(form.get("price"))
        qty = parse_quantity(form.get("quantity"))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("bills.show", bill_id=bill.id))
    if price is None:
        flash("Podaj kwotę pozycji.", "danger")
        return redirect(url_for("bills.show", bill_id=bill.id))
    pos = max((p.position for p in bill.products), default=-1) + 1
    bill.products.append(Product(
        name=clean(form.get("name")) or "Pozycja", quantity=qty, price=price, position=pos,
        consumers=selected_people(form, "consumers", bill.trip.participants),
    ))
    db.session.commit()
    return redirect(url_for("bills.show", bill_id=bill.id) + "#add-product")


@bp.post("/products/<int:product_id>/delete")
def delete_product(product_id: int):
    prod = db.get_or_404(Product, product_id)
    bill_id = prod.bill_id
    db.session.delete(prod)
    db.session.commit()
    back = request.form.get("back")
    if back == "table":
        return redirect(url_for("trips.table", trip_id=db.session.get(Bill, bill_id).trip_id))
    return redirect(url_for("bills.show", bill_id=bill_id))


@bp.post("/bills/<int:bill_id>/consumers")
def bulk_consumers(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    people = selected_people(request.form, "consumers", bill.trip.participants)
    for prod in bill.products:
        prod.consumers = list(people)
    db.session.commit()
    names = ", ".join(p.name for p in people) or "nikogo"
    flash(f"Wszystkie pozycje przypisano do: {names}.", "success")
    return redirect(url_for("bills.show", bill_id=bill.id))


@bp.post("/bills/<int:bill_id>/photos")
def add_photos(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    try:
        _save_photos(bill)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("bills.show", bill_id=bill.id))
    db.session.commit()
    flash("Dodano zdjęcia.", "success")
    return redirect(url_for("bills.show", bill_id=bill.id))


@bp.post("/attachments/<int:att_id>/delete")
def delete_photo(att_id: int):
    att = db.get_or_404(Attachment, att_id)
    bill_id = att.bill_id
    remove_files([att])
    db.session.delete(att)
    db.session.commit()
    flash("Usunięto zdjęcie.", "success")
    return redirect(url_for("bills.show", bill_id=bill_id))


@bp.post("/bills/<int:bill_id>/ai")
def reparse(bill_id: int):
    bill = db.get_or_404(Bill, bill_id)
    if not bill.attachments:
        flash("Ten rachunek nie ma zdjęć.", "warning")
        return redirect(url_for("bills.show", bill_id=bill.id))
    replace = request.form.get("replace", "1") == "1"
    if _run_claude(bill, list(bill.trip.participants), replace=replace):
        db.session.commit()
    else:
        db.session.rollback()
    return redirect(url_for("bills.show", bill_id=bill.id))


@bp.get("/uploads/<name>")
def upload(name: str):
    if not _UPLOAD_RE.match(name):
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_DIR"], name, max_age=86400 * 30)
