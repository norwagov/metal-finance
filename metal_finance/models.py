from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

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

    @property
    def total(self) -> int:
        return sum(b.total for b in self.bills)


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

    @property
    def total(self) -> int:
        return sum(p.price for p in self.products)

    @property
    def unassigned_count(self) -> int:
        return sum(1 for p in self.products if not p.consumers)

    @property
    def total_mismatch(self) -> int | None:
        if self.declared_total is None or not self.products:
            return None
        diff = self.total - self.declared_total
        return diff or None


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
    if "weighted" not in {c["name"] for c in insp.get_columns("product")}:
        with db.engine.begin() as conn:
            conn.execute(db.text("ALTER TABLE product ADD COLUMN weighted BOOLEAN NOT NULL DEFAULT 0"))


def seed_defaults() -> None:
    if Category.query.count() == 0:
        for name, icon, color in DEFAULT_CATEGORIES:
            db.session.add(Category(name=name, icon=icon, color=color))
        db.session.commit()
