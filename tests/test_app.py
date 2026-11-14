import io
import re
from fractions import Fraction

import pytest
from PIL import Image

from metal_finance import create_app, receipt_ai
from metal_finance.models import Bill, Category, Payment, Person, Product, Trip, db
from metal_finance.money import fmt_amount, parse_money, round_cents
from metal_finance.settlement import summarize


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "UPLOAD_DIR": str(tmp_path / "uploads"),
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}"})
    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.get("/")  # ustawia token CSRF w sesji
    return c


def csrf(client):
    with client.session_transaction() as s:
        return s["csrf"]


# --- pieniądze --------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,cents", [
    ("12,50", 1250), ("12.5", 1250), ("1 234,56 zł", 123456), ("1.234,56", 123456),
    ("1,234.56", 123456), (12.345, 1235), ("-3,10", -310), ("", None), (None, None),
])
def test_parse_money(raw, cents):
    assert parse_money(raw) == cents


def test_format():
    assert fmt_amount(123456, "PLN") == "1 234,56 zł"
    assert fmt_amount(-5) == "−0,05"
    assert round_cents(Fraction(1000, 3)) == 333
    assert round_cents(Fraction(-1, 2)) == -1


# --- rozliczenie --------------------------------------------------------------------------------

def make_trip():
    a, b, c = Person(name="Ania"), Person(name="Bartek"), Person(name="Czarek")
    t = Trip(name="Test", participants=[a, b, c])
    db.session.add(t)
    b1 = Bill(trip=t, title="Paliwo", payer=a)
    b1.products.append(Product(name="Pb95", price=30000, consumers=[a, b, c]))  # po 100 zł
    b2 = Bill(trip=t, title="Piwo", payer=b)
    b2.products.append(Product(name="Piwo", price=1000, consumers=[a, c]))  # po 5 zł
    b2.products.append(Product(name="Nikt", price=999, consumers=[]))  # nieprzypisane
    db.session.add_all([b1, b2])
    db.session.commit()
    return t, a, b, c


def test_summary(app):
    with app.app_context():
        t, a, b, c = make_trip()
        s = summarize(t)
        assert s.total == 31999
        assert len(s.unassigned) == 1
        assert {r.person.name: r.balance for r in s.people} == {"Ania": 19500, "Bartek": -9000, "Czarek": -10500}
        assert sum(r.balance for r in s.people) == 0
        tr = {(x.sender.name, x.receiver.name): x.amount for x in s.transfers}
        assert tr == {("Czarek", "Ania"): 10500, ("Bartek", "Ania"): 9000}

        db.session.add(Payment(trip=t, sender=c, receiver=a, amount=10500))
        db.session.commit()
        s = summarize(t)
        assert [(x.sender.name, x.receiver.name, x.amount) for x in s.transfers] == [("Bartek", "Ania", 9000)]


def test_rounding_sums_to_zero(app):
    with app.app_context():
        people = [Person(name=n) for n in "ABC"]
        t = Trip(name="R", participants=people)
        bl = Bill(trip=t, title="x", payer=people[0])
        bl.products.append(Product(name="x", price=1000, consumers=people))  # 10 zł / 3
        db.session.add(bl)
        db.session.commit()
        s = summarize(t)
        assert sum(r.balance for r in s.people) == 0
        assert sum(x.amount for x in s.transfers) == s.people[0].balance


# --- widoki -------------------------------------------------------------------------------------

def test_full_flow(app, client):
    tok = csrf(client)
    r = client.post("/trips/new", data={"_csrf": tok, "name": "Mazury", "currency": "PLN", "new_people": "Ania, Bartek"})
    assert r.status_code == 302
    trip_id = int(re.search(r"/trips/(\d+)", r.location).group(1))
    with app.app_context():
        t = db.session.get(Trip, trip_id)
        a, b = t.participants
        cat = Category.query.filter_by(name="Paliwo").first()
        ids = (a.id, b.id, cat.id)

    for path in ("/", f"/trips/{trip_id}", f"/trips/{trip_id}/table", f"/trips/{trip_id}/settle",
                 f"/trips/{trip_id}/bills/new?mode=manual", f"/trips/{trip_id}/bills/new?mode=ai",
                 "/categories", "/people", "/settings"):
        assert client.get(path).status_code == 200, path

    # szybki rachunek z kwotą
    r = client.post(f"/trips/{trip_id}/bills/new", data={
        "_csrf": tok, "mode": "manual", "title": "Orlen", "category_id": ids[2], "payer_id": ids[0],
        "amount": "200,00", "consumers": [ids[0], ids[1]], "next": "trip"})
    assert r.status_code == 302
    with app.app_context():
        bill = Bill.query.filter_by(title="Orlen").one()
        prod = bill.products[0]
        assert prod.price == 20000 and len(prod.consumers) == 2
        pid, bid = prod.id, bill.id

    assert client.get(f"/bills/{bid}").status_code == 200

    # API: odznacz Bartka -> Ania płaci sama za siebie
    r = client.post(f"/api/products/{pid}/consumers", json={"person_id": ids[1], "on": False}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and r.json["ok"]
    assert r.json["bind"][f"balance:{ids[1]}"].startswith("0,00")
    r = client.post(f"/api/products/{pid}/consumers", json={"set": "all"}, headers={"X-CSRF-Token": tok})
    assert r.json["bind"][f"share:{pid}:{ids[1]}"] == "100,00"
    r = client.post(f"/api/products/{pid}", json={"price": "150,5"}, headers={"X-CSRF-Token": tok})
    assert r.json["values"]["price"] == "150,50"
    r = client.post(f"/api/products/{pid}", json={"price": "abc"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 400

    # bez tokenu CSRF -> 400
    assert client.post(f"/api/products/{pid}", json={"price": "1"}).status_code == 400

    # pozycja ręcznie + spłata + CSV
    r = client.post(f"/bills/{bid}/products", data={"_csrf": tok, "name": "Hot-dog", "quantity": "2", "price": "17,98", "consumers": [ids[1]]})
    assert r.status_code == 302
    r = client.post(f"/trips/{trip_id}/payments", data={"_csrf": tok, "from_id": ids[1], "to_id": ids[0], "amount": "10"})
    assert r.status_code == 302
    r = client.get(f"/trips/{trip_id}/export.csv")
    assert r.status_code == 200 and "Hot-dog" in r.get_data(as_text=True)
    assert client.get(f"/trips/{trip_id}/settle").status_code == 200


def test_ai_flow_with_photo(app, client, monkeypatch):
    tok = csrf(client)
    client.post("/trips/new", data={"_csrf": tok, "name": "AI", "new_people": "X, Y"})
    with app.app_context():
        t = Trip.query.filter_by(name="AI").one()
        trip_id, payer = t.id, t.participants[0].id

    seen = {}

    def fake_parse(paths, cats, currency):
        seen["paths"] = paths
        return receipt_ai.normalize({
            "merchant": "Żabka", "date": "2026-07-01", "currency": "PLN", "total": 21.48, "category": "zakupy",
            "items": [{"name": "Piwo", "quantity": 2, "price": 9.98}, {"name": "Chipsy", "price": "11,50"}],
            "notes": None,
        })

    monkeypatch.setattr(receipt_ai, "parse_receipt", fake_parse)
    buf = io.BytesIO()
    Image.new("RGB", (40, 80), "white").save(buf, "PNG")
    buf.seek(0)
    r = client.post(f"/trips/{trip_id}/bills/new", data={
        "_csrf": tok, "mode": "ai", "payer_id": payer, "consumers": [payer], "photos": (buf, "paragon.png")},
        content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        bill = Bill.query.filter_by(trip_id=trip_id).one()
        assert bill.title == "Żabka" and bill.source == "ai"
        assert [p.price for p in bill.products] == [998, 1150]
        assert bill.category.name == "Zakupy"
        assert bill.declared_total == 2148 and bill.total_mismatch is None
        assert len(bill.attachments) == 1
        fname = bill.attachments[0].filename
    assert seen["paths"][0].endswith(".jpg")
    assert client.get(f"/uploads/{fname}").status_code == 200


def test_ai_without_token_keeps_bill(app, client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    tok = csrf(client)
    client.post("/trips/new", data={"_csrf": tok, "name": "NoTok", "new_people": "Q"})
    with app.app_context():
        t = Trip.query.filter_by(name="NoTok").one()
        trip_id, payer = t.id, t.participants[0].id
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "JPEG")
    buf.seek(0)
    r = client.post(f"/trips/{trip_id}/bills/new", data={"_csrf": tok, "mode": "ai", "payer_id": payer, "photos": (buf, "a.jpg")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    assert "Brak tokenu subskrypcji Claude" in r.get_data(as_text=True)
    with app.app_context():
        assert Bill.query.filter_by(trip_id=trip_id).one().title == "Paragon"


def test_password_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "metal")
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'p.db'}"})
    c = app.test_client()
    assert c.get("/").status_code == 302
    c.get("/login")
    with c.session_transaction() as s:
        tok = s["csrf"]
    assert c.post("/login", data={"_csrf": tok, "password": "zle"}).status_code == 200
    assert c.post("/login", data={"_csrf": tok, "password": "metal"}).status_code == 302
    assert c.get("/").status_code == 200


def test_scrub_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    receipt_ai._scrub_env()
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ
