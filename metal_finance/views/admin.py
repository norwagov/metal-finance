from __future__ import annotations

import hmac
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from .. import receipt_ai
from ..models import Bill, Category, Payment, Person, Product, db, product_consumers, trip_participants
from . import clean

bp = Blueprint("admin", __name__)

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


# --- logowanie (opcjonalne, gdy ustawiono APP_PASSWORD) ---------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config["APP_PASSWORD"]:
        return redirect(url_for("trips.index"))
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), current_app.config["APP_PASSWORD"]):
            session.permanent = True
            session["auth_ok"] = True
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("trips.index")
            return redirect(nxt)
        flash("Złe hasło.", "danger")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


# --- kategorie --------------------------------------------------------------------------------

@bp.route("/categories", methods=["GET", "POST"])
def categories():
    if request.method == "POST":
        name = clean(request.form.get("name"), 60)
        if not name:
            flash("Podaj nazwę kategorii.", "danger")
        elif any(c.name.casefold() == name.casefold() for c in Category.query.all()):
            flash(f"Kategoria „{name}” już istnieje.", "warning")
        else:
            db.session.add(Category(name=name, icon=_icon(request.form.get("icon")), color=_color(request.form.get("color"))))
            db.session.commit()
            flash(f"Dodano kategorię „{name}”.", "success")
        return redirect(url_for("admin.categories"))
    cats = Category.query.order_by(Category.name).all()
    counts = dict(db.session.query(Bill.category_id, db.func.count(Bill.id)).group_by(Bill.category_id).all())
    return render_template("categories.html", categories=cats, counts=counts)


@bp.post("/categories/<int:cat_id>")
def update_category(cat_id: int):
    cat = db.get_or_404(Category, cat_id)
    name = clean(request.form.get("name"), 60)
    if name and not any(c.id != cat.id and c.name.casefold() == name.casefold() for c in Category.query.all()):
        cat.name = name
    elif name:
        flash(f"Kategoria „{name}” już istnieje.", "warning")
    cat.icon = _icon(request.form.get("icon"))
    cat.color = _color(request.form.get("color"))
    db.session.commit()
    flash("Zapisano kategorię.", "success")
    return redirect(url_for("admin.categories"))


@bp.post("/categories/<int:cat_id>/delete")
def delete_category(cat_id: int):
    cat = db.get_or_404(Category, cat_id)
    Bill.query.filter_by(category_id=cat.id).update({"category_id": None})
    db.session.delete(cat)
    db.session.commit()
    flash(f"Usunięto kategorię „{cat.name}” (rachunki zostały bez kategorii).", "success")
    return redirect(url_for("admin.categories"))


def _icon(value: str | None) -> str:
    v = (value or "").strip()
    return v[:8] or "🧾"


def _color(value: str | None) -> str:
    v = (value or "").strip()
    return v if _COLOR_RE.match(v) else "#6b7280"


# --- osoby ------------------------------------------------------------------------------------

def _usage(person: Person) -> int:
    return (
        db.session.query(trip_participants).filter_by(person_id=person.id).count()
        + Bill.query.filter_by(payer_id=person.id).count()
        + db.session.query(product_consumers).filter_by(person_id=person.id).count()
        + Payment.query.filter((Payment.from_id == person.id) | (Payment.to_id == person.id)).count()
    )


@bp.route("/people", methods=["GET", "POST"])
def people():
    if request.method == "POST":
        name = clean(request.form.get("name"), 80)
        if not name:
            flash("Podaj imię.", "danger")
        elif any(p.name.casefold() == name.casefold() for p in Person.query.all()):
            flash(f"{name} już jest na liście.", "warning")
        else:
            db.session.add(Person(name=name))
            db.session.commit()
            flash(f"Dodano: {name}.", "success")
        return redirect(url_for("admin.people"))
    persons = Person.query.order_by(Person.name).all()
    trips_count = dict(
        db.session.query(trip_participants.c.person_id, db.func.count()).group_by(trip_participants.c.person_id).all()
    )
    return render_template("people.html", people=persons, trips_count=trips_count)


@bp.post("/people/<int:person_id>")
def rename_person(person_id: int):
    person = db.get_or_404(Person, person_id)
    name = clean(request.form.get("name"), 80)
    if not name:
        flash("Imię nie może być puste.", "danger")
    elif any(p.id != person.id and p.name.casefold() == name.casefold() for p in Person.query.all()):
        flash(f"{name} już jest na liście.", "warning")
    else:
        person.name = name
        db.session.commit()
        flash("Zapisano.", "success")
    return redirect(url_for("admin.people"))


@bp.post("/people/<int:person_id>/delete")
def delete_person(person_id: int):
    person = db.get_or_404(Person, person_id)
    if _usage(person):
        flash(f"{person.name} bierze udział w wyjazdach - najpierw usuń go/ją z nich.", "warning")
    else:
        db.session.delete(person)
        db.session.commit()
        flash(f"Usunięto: {person.name}.", "success")
    return redirect(url_for("admin.people"))


# --- ustawienia / status Claude -----------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    result = None
    if request.method == "POST":
        try:
            answer = receipt_ai.ping()
            result = ("success", f"Połączenie działa. Claude odpowiedział: „{answer[:80]}”.")
        except receipt_ai.ReceiptError as exc:
            result = ("danger", str(exc))
    stats = {
        "products": Product.query.count(),
        "bills": Bill.query.count(),
        "people": Person.query.count(),
    }
    return render_template(
        "settings.html", claude_ready=receipt_ai.is_configured(), model=receipt_ai.model_name(),
        result=result, stats=stats, data_dir=current_app.config["DATA_DIR"],
    )
