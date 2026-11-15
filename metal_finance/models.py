from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .money import round_cents

db = SQLAlchemy()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


trip_participants = db.Table(
    "trip_participants",
    db.Column("trip_id", db.ForeignKey("trip.id", ondelete="CASCADE"), primary_key=True),
    db.Column("person_id", db.ForeignKey("person.id", ondelete="CASCADE"), primary_key=True),
)

product_consumers = db.Table(
    "product_consumers",
    db.Column("product_id", db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True),
    db.Column("person_id", db.ForeignKey("person.id", ondelete="CASCADE"), primary_key=True),
)

AVATAR_COLORS = [
    "#e8541e", "#3b82f6", "#10b981", "#a855f7", "#f59e0b",
    "#ef4444", "#06b6d4", "#84cc16", "#ec4899", "#8b5cf6",
]


class Person(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    @property
    def color(self) -> str:
        # kolejne osoby dostają kolejne kolory - pierwsze 10 osób zawsze się różni
        if self.id:
            return AVATAR_COLORS[(self.id - 1) % len(AVATAR_COLORS)]
        h = int(hashlib.md5(self.name.encode()).hexdigest(), 16)
        return AVATAR_COLORS[h % len(AVATAR_COLORS)]

    @property
    def initials(self) -> str:
        parts = self.name.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return self.name[:2].upper()


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, nullable=False)
    icon = db.Column(db.String(16), default="🧾", nullable=False)
    color = db.Column(db.String(7), default="#6b7280", nullable=False)


class Trip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, default="")
    currency = db.Column(db.String(3), default="PLN", nullable=False)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=_utcnow)

    participants = db.relationship("Person", secondary=trip_participants, order_by="Person.name")
    bills = db.relationship(
        "Bill", back_populates="trip", cascade="all, delete-orphan",
        order_by="(Bill.date.desc(), Bill.id.desc())",
    )
    payments = db.relationship(
        "Payment", back_populates="trip", cascade="all, delete-orphan",
        order_by="(Payment.date, Payment.id)",
    )
    rates = db.relationship("TripRate", cascade="all, delete-orphan", order_by="TripRate.currency")

    def rate_for(self, currency: str | None) -> TripRate | None:
        for r in self.rates:
            if r.currency == currency:
                return r
        return None

    @property
    def foreign_currencies(self) -> list[str]:
        """Waluty rachunków inne niż waluta wyjazdu (w kolejności alfabetycznej)."""
        return sorted({b.currency for b in self.bills if b.currency and b.currency != self.currency})

    @property
    def rate_rows(self) -> list[tuple[str, TripRate | None]]:
        """(waluta, kurs) dla każdej obcej waluty użytej w rachunkach."""
        return [(c, self.rate_for(c)) for c in self.foreign_currencies]

    @property
    def total(self) -> int:
        """Suma rachunków w walucie wyjazdu (rachunki bez kursu pomijamy)."""
        return round_cents(sum((b.total_converted for b in self.bills if b.total_converted is not None), Fraction(0)))

    @property
    def missing_rate_bills(self) -> list:
        return [b for b in self.bills if b.missing_rate]


class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.ForeignKey("trip.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False, default="")
    category_id = db.Column(db.ForeignKey("category.id", ondelete="SET NULL"))
    payer_id = db.Column(db.ForeignKey("person.id"), nullable=False)
    date = db.Column(db.Date, default=date.today)
    note = db.Column(db.Text, default="")
    declared_total = db.Column(db.Integer)  # suma z paragonu (wg AI lub wpisana) do kontroli
    source = db.Column(db.String(10), default="manual")  # manual | ai
    created_at = db.Column(db.DateTime, default=_utcnow)
    # Kwoty pozycji są w walucie rachunku; do rozliczenia przeliczamy je kursem wyjazdu (TripRate).
    currency = db.Column(db.String(3), nullable=False, default="PLN", server_default="PLN")

    trip = db.relationship("Trip", back_populates="bills")
    category = db.relationship("Category")
    payer = db.relationship("Person")
    products = db.relationship(
        "Product", back_populates="bill", cascade="all, delete-orphan",
        order_by="(Product.position, Product.id)",
    )
    attachments = db.relationship(
        "Attachment", back_populates="bill", cascade="all, delete-orphan",
        order_by="Attachment.id",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not kwargs.get("currency") and self.trip is not None:
            self.currency = self.trip.currency

    @property
    def total(self) -> int:
        """Suma pozycji w walucie rachunku."""
        return sum(p.price for p in self.products)

    @property
    def trip_currency(self) -> str:
        return self.trip.currency if self.trip else self.currency

    @property
    def is_foreign(self) -> bool:
        return bool(self.currency) and self.currency != self.trip_currency

    @property
    def trip_rate(self) -> TripRate | None:
        return self.trip.rate_for(self.currency) if self.is_foreign and self.trip else None

    @property
    def rate_value(self) -> Decimal | None:
        if not self.is_foreign:
            return Decimal(1)
        tr = self.trip_rate
        return tr.value if tr else None

    @property
    def rate(self) -> Fraction | None:
        """Kurs do przeliczeń (dokładny ułamek); None = rachunek w obcej walucie bez kursu."""
        value = self.rate_value
        return Fraction(value) if value is not None else None

    @property
    def missing_rate(self) -> bool:
        return self.rate is None

    def convert(self, cents) -> Fraction | None:
        """Kwota w walucie rachunku -> dokładna kwota w walucie wyjazdu."""
        rate = self.rate
        return None if rate is None else Fraction(cents) * rate

    @property
    def total_converted(self) -> Fraction | None:
        return self.convert(self.total)

    @property
    def unassigned_count(self) -> int:
        return sum(1 for p in self.products if not p.consumers)

    @property
    def total_mismatch(self) -> int | None:
        if self.declared_total is None or not self.products:
            return None
        diff = self.total - self.declared_total
        return diff or None


class TripRate(db.Model):
    """Kurs waluty w danym wyjeździe: 1 `currency` = `rate` waluty wyjazdu.

    Pobierany z NBP raz - gdy waluta pierwszy raz pojawia się w rachunkach - potem zmienia się
    tylko ręcznie albo przyciskiem „odśwież”.
    """
    __table_args__ = (db.UniqueConstraint("trip_id", "currency"),)

    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.ForeignKey("trip.id", ondelete="CASCADE"), nullable=False, index=True)
    currency = db.Column(db.String(3), nullable=False)
    rate = db.Column(db.String(24), nullable=False)  # Decimal jako tekst
    rate_date = db.Column(db.Date)  # data notowania (dla kursu z NBP/EBC)
    source = db.Column(db.String(10))  # NBP | EBC | manual
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    @property
    def value(self) -> Decimal:
        return Decimal(self.rate)

    def set(self, value: Decimal, day: date | None, source: str) -> None:
        self.rate = format(Decimal(value).normalize(), "f")
        self.rate_date = day
        self.source = source


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.ForeignKey("bill.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False, default="")
    quantity = db.Column(db.Float, default=1.0, nullable=False)
    price = db.Column(db.Integer, nullable=False, default=0)  # kwota za całą pozycję, w groszach
    position = db.Column(db.Integer, default=0, nullable=False)
    # True = koszt dzieli się proporcjonalnie do wag konsumentów zamiast po równo
    weighted = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    bill = db.relationship("Bill", back_populates="products")
    consumers = db.relationship("Person", secondary=product_consumers, order_by="Person.name")
    weights = db.relationship("ProductWeight", cascade="all, delete-orphan", lazy="selectin")

    def weight_of(self, person_id: int) -> float:
        """Waga osoby w tej pozycji (domyślnie 1)."""
        for w in self.weights:
            if w.person_id == person_id:
                return w.weight
        return 1.0

    @property
    def weights_locked(self) -> bool:
        """Wag nie da się wyłączyć, dopóki zmieniają podział (konsumenci mają różne wagi)."""
        if not self.weighted:
            return False
        return len({self.weight_of(c.id) for c in self.consumers}) > 1

    def set_weight(self, person_id: int, weight: float) -> None:
        for w in self.weights:
            if w.person_id == person_id:
                w.weight = weight
                return
        self.weights.append(ProductWeight(person_id=person_id, weight=weight))


class ProductWeight(db.Model):
    """Waga osoby w pozycji - używana tylko, gdy pozycja ma włączone wagi (Product.weighted)."""
    product_id = db.Column(db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True)
    person_id = db.Column(db.ForeignKey("person.id", ondelete="CASCADE"), primary_key=True)
    weight = db.Column(db.Float, nullable=False, default=1.0)


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.ForeignKey("bill.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    bill = db.relationship("Bill", back_populates="attachments")


class Payment(db.Model):
    """Spłata między uczestnikami (np. przelew BLIK po wyjeździe)."""
    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.ForeignKey("trip.id", ondelete="CASCADE"), nullable=False, index=True)
    from_id = db.Column(db.ForeignKey("person.id"), nullable=False)
    to_id = db.Column(db.ForeignKey("person.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    date = db.Column(db.Date, default=date.today)
    note = db.Column(db.String(200), default="")

    trip = db.relationship("Trip", back_populates="payments")
    sender = db.relationship("Person", foreign_keys=[from_id])
    receiver = db.relationship("Person", foreign_keys=[to_id])


DEFAULT_CATEGORIES = [
    ("Paliwo", "⛽", "#f59e0b"),
    ("Jedzenie", "🍔", "#10b981"),
    ("Alkohol", "🍺", "#eab308"),
    ("Zakupy", "🛒", "#06b6d4"),
    ("Nocleg", "🏨", "#8b5cf6"),
    ("Transport", "🚗", "#3b82f6"),
    ("Bilety i atrakcje", "🎸", "#ef4444"),
    ("Inne", "🧾", "#6b7280"),
]


def upgrade_schema() -> None:
    """Dokłada kolumny dodane po pierwszym wydaniu (create_all nie zmienia istniejących tabel)."""
    insp = db.inspect(db.engine)
    added = {
        "product": [("weighted", "BOOLEAN NOT NULL DEFAULT 0")],
        "bill": [("currency", "VARCHAR(3) NOT NULL DEFAULT 'PLN'")],
    }
    for table, columns in added.items():
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name in have:
                continue
            with db.engine.begin() as conn:
                conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                if (table, name) == ("bill", "currency"):
                    # dotychczasowe rachunki były zawsze w walucie wyjazdu
                    conn.execute(db.text(
                        "UPDATE bill SET currency = (SELECT trip.currency FROM trip WHERE trip.id = bill.trip_id)"
                    ))
    _migrate_bill_rates(insp)


def _migrate_bill_rates(insp) -> None:
    """Kursy zapisane kiedyś przy rachunkach (bill.exchange_rate) -> jeden kurs na walutę wyjazdu.

    Bierzemy kurs z najwcześniej dodanego rachunku w danej walucie (ręczny ma pierwszeństwo),
    a stare kolumny czyścimy, żeby migracja nie wróciła przy następnym starcie.
    """
    if "exchange_rate" not in {c["name"] for c in insp.get_columns("bill")}:
        return
    with db.engine.begin() as conn:
        rows = conn.execute(db.text(
            "SELECT b.trip_id, b.currency, b.exchange_rate, b.rate_date, b.rate_source FROM bill b "
            "JOIN trip t ON t.id = b.trip_id WHERE b.exchange_rate IS NOT NULL AND b.currency != t.currency "
            "ORDER BY (b.rate_source = 'manual') DESC, b.id"
        )).fetchall()
        seen = {tuple(r) for r in conn.execute(db.text("SELECT trip_id, currency FROM trip_rate"))}
        for trip_id, currency, rate, rate_date, source in rows:
            if (trip_id, currency) in seen:
                continue
            seen.add((trip_id, currency))
            conn.execute(
                db.text("INSERT INTO trip_rate (trip_id, currency, rate, rate_date, source, updated_at) "
                        "VALUES (:t, :c, :r, :d, :s, :u)"),
                {"t": trip_id, "c": currency, "r": rate, "d": rate_date, "s": source or "NBP", "u": _utcnow().isoformat(" ")},
            )
        conn.execute(db.text("UPDATE bill SET exchange_rate = NULL WHERE exchange_rate IS NOT NULL"))


def seed_defaults() -> None:
    if Category.query.count() == 0:
        for name, icon, color in DEFAULT_CATEGORIES:
            db.session.add(Category(name=name, icon=icon, color=color))
        db.session.commit()
