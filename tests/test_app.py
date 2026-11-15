import io
import re
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from PIL import Image

from metal_finance import create_app, fx, receipt_ai
from metal_finance.models import Bill, Category, Payment, Person, Product, Trip, TripRate, db
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


FAKE_NBP = {"EUR": "4.3000", "CZK": "0.1720", "USD": "3.9000"}


@pytest.fixture(autouse=True)
def fake_rates(monkeypatch):
    """Testy nie chodzą do internetu: udajemy NBP z kilkoma kursami (tabela A, notowanie 2026-07-01)."""
    calls = []

    def fake_get_json(url):
        calls.append(url)
        if "api.nbp.pl" in url:
            m = re.search(r"/rates/([AB])/(\w{3})/", url)
            table, code = m.group(1), m.group(2).upper()
            if table == "A" and code in FAKE_NBP:
                return {"rates": [{"effectiveDate": "2026-06-30", "mid": Decimal("1")},
                                  {"effectiveDate": "2026-07-01", "mid": Decimal(FAKE_NBP[code])}]}
        return None

    fx.clear_cache()
    monkeypatch.setattr(fx, "_get_json", fake_get_json)
    yield calls
    fx.clear_cache()


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
    assert "nie trzeba żadnych kursów" in client.get(f"/trips/{trip_id}/rates").get_data(as_text=True)


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


# --- wagi osób w pozycji ----------------------------------------------------------------------

def test_weighted_shares(app):
    with app.app_context():
        t, a, b, c = make_trip()
        prod = next(p for bl in t.bills for p in bl.products if p.name == "Pb95")
        prod.weighted = True
        prod.set_weight(a.id, 2)
        prod.set_weight(b.id, 1.5)
        db.session.commit()
        from metal_finance.settlement import product_shares
        sh = product_shares(prod)  # 300 zł w proporcji 2 : 1,5 : 1
        assert sh == {a.id: Fraction(40000, 3), b.id: Fraction(10000), c.id: Fraction(20000, 3)}
        s = summarize(t)
        assert sum(r.balance for r in s.people) == 0
        # wyłączenie wag wraca do podziału po równo, wagi zostają zapamiętane
        prod.weighted = False
        assert set(product_shares(prod).values()) == {Fraction(10000)}
        assert prod.weight_of(a.id) == 2
        # same zera -> po równo
        prod.weighted = True
        for p in (a, b, c):
            prod.set_weight(p.id, 0)
        assert set(product_shares(prod).values()) == {Fraction(10000)}


def test_weights_api(app, client):
    with app.app_context():
        t, a, b, c = make_trip()
        prod = next(p for bl in t.bills for p in bl.products if p.name == "Pb95")
        pid, ids, bill_id = prod.id, (a.id, b.id, c.id), prod.bill_id
    tok = {"X-CSRF-Token": csrf(client)}
    html = client.get(f"/bills/{bill_id}").get_data(as_text=True)
    assert "js-weighted" in html and "js-weight" in html

    r = client.post(f"/api/products/{pid}", json={"weighted": True}, headers=tok).get_json()
    assert r["rows"][str(pid)]["weighted"] is True
    r = client.post(f"/api/products/{pid}/weights", json={"person_id": ids[0], "weight": "2"}, headers=tok).get_json()
    assert r["ok"] and r["rows"][str(pid)]["weights"][str(ids[0])] == "2"
    assert r["bind"][f"share:{pid}:{ids[0]}"] == "150,00"
    assert r["bind"][f"share:{pid}:{ids[1]}"] == "75,00"
    r = client.post(f"/api/products/{pid}/weights", json={"person_id": ids[1], "weight": "0,5"}, headers=tok).get_json()
    assert r["rows"][str(pid)]["weights"][str(ids[1])] == "0,5"

    assert client.post(f"/api/products/{pid}/weights", json={"person_id": ids[0], "weight": "abc"}, headers=tok).status_code == 400
    assert client.post(f"/api/products/{pid}/weights", json={"person_id": ids[0], "weight": "-1"}, headers=tok).status_code == 400
    assert client.post(f"/api/products/{pid}/weights", json={"person_id": 9999, "weight": "1"}, headers=tok).status_code == 400
    html = client.get(f"/bills/{bill_id}").get_data(as_text=True)
    assert re.search(rf'data-product="{pid}" class="product-row[^"]*\bweighted\b', html)

    # różne wagi -> nie da się wyłączyć
    assert r["rows"][str(pid)]["locked"] is True
    html = client.get(f"/bills/{bill_id}").get_data(as_text=True)
    assert re.search(r'js-weighted[^"]*\blocked\b', html)
    r = client.post(f"/api/products/{pid}", json={"weighted": False}, headers=tok)
    assert r.status_code == 400 and "tę samą wagę" in r.get_json()["error"]

    # waga 0 też zmienia podział
    for i, w in ((0, "1"), (1, "0")):
        r = client.post(f"/api/products/{pid}/weights", json={"person_id": ids[i], "weight": w}, headers=tok).get_json()
    assert r["rows"][str(pid)]["locked"] is True

    # 1 1 1 -> można wyłączyć
    r = client.post(f"/api/products/{pid}/weights", json={"person_id": ids[1], "weight": "1"}, headers=tok).get_json()
    assert r["rows"][str(pid)]["locked"] is False
    r = client.post(f"/api/products/{pid}", json={"weighted": False}, headers=tok).get_json()
    assert r["ok"] and r["rows"][str(pid)]["weighted"] is False
    assert r["bind"][f"share:{pid}:{ids[0]}"] == "100,00"


def test_upgrade_schema_adds_column(tmp_path, monkeypatch):
    import sqlite3
    dbfile = tmp_path / "old.db"
    con = sqlite3.connect(dbfile)
    con.execute("CREATE TABLE product (id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL, name VARCHAR(200) NOT NULL,"
                " quantity FLOAT NOT NULL, price INTEGER NOT NULL, position INTEGER NOT NULL)")
    con.commit()
    con.close()
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SQLALCHEMY_DATABASE_URI": f"sqlite:///{dbfile}"})
    cols = [r[1] for r in sqlite3.connect(dbfile).execute("PRAGMA table_info(product)")]
    assert "weighted" in cols


# --- waluty ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,cents", [("12,50 €", 1250), ("1 234,00 Kč", 123400), ("EUR 7.5", 750), ("15 kr", 1500)])
def test_parse_money_foreign(raw, cents):
    assert parse_money(raw) == cents


def test_rates_nbp_direct_and_cross():
    r = fx.get_rate("EUR", "PLN", date(2026, 7, 3))
    assert (r.value, r.day, r.source) == (Decimal("4.3"), date(2026, 7, 1), "NBP")
    assert fx.get_rate("PLN", "EUR", date(2026, 7, 3)).value == Decimal("0.2325581395")
    assert fx.get_rate("CZK", "EUR", date(2026, 7, 3)).value == Decimal("0.04")
    assert fx.get_rate("EUR", "EUR").value == 1
    with pytest.raises(fx.RateError):
        fx.get_rate("XXX", "PLN", date(2026, 7, 3))


def test_rates_fallback_to_ecb(monkeypatch):
    def fake(url):
        if "nbp" in url:
            raise fx.RateError("brak połączenia z serwerem kursów")
        return {"date": "2026-07-01", "rates": {"PLN": Decimal("4.2500")}}
    monkeypatch.setattr(fx, "_get_json", fake)
    r = fx.get_rate("EUR", "PLN", date(2026, 7, 1))
    assert (r.value, r.source) == (Decimal("4.25"), "EBC")


def test_summary_converts_foreign_bills(app):
    with app.app_context():
        t, a, b, c = make_trip()
        eur = Bill(trip=t, title="Pizza w Wiedniu", payer=c, currency="EUR")
        eur.products.append(Product(name="Pizza", price=3000, consumers=[a, b, c]))  # 30 € = 129 zł, po 43 zł
        db.session.add(eur)
        db.session.commit()
        s = summarize(t)  # brak kursu -> rachunek pominięty i oznaczony
        assert s.total == 31999 and s.missing_rate == [eur]
        assert sum(r.balance for r in s.people) == 0

        tr = TripRate(currency="EUR")
        tr.set(Decimal("4.3"), date(2026, 7, 1), "NBP")
        t.rates.append(tr)
        db.session.commit()
        s = summarize(t)
        assert s.total == 31999 + 12900 and not s.missing_rate
        assert {r.person.name: r.balance for r in s.people} == {"Ania": 15200, "Bartek": -13300, "Czarek": -1900}
        assert s.row(c.id).paid == 12900
        assert eur.total == 3000 and eur.total_converted == 12900


def _trip(client, app, name, currency="PLN"):
    tok = csrf(client)
    client.post("/trips/new", data={"_csrf": tok, "name": name, "currency": currency, "new_people": "Ania, Bartek"})
    with app.app_context():
        t = Trip.query.filter_by(name=name).one()
        return tok, t.id, [p.id for p in t.participants]


def _manual_bill(client, tok, trip_id, payer, consumers, title, amount, currency, day="2026-07-04"):
    return client.post(f"/trips/{trip_id}/bills/new", data={
        "_csrf": tok, "mode": "manual", "payer_id": payer, "consumers": consumers, "title": title,
        "date": day, "amount": amount, "currency": currency, "next": "trip"}, follow_redirects=True)


def _rates(app, trip_id):
    with app.app_context():
        return {r.currency: (r.rate, r.source) for r in db.session.get(Trip, trip_id).rates}


def test_trip_rate_fetched_once(app, client, fake_rates):
    tok, trip_id, (a, b) = _trip(client, app, "Praga")
    html = _manual_bill(client, tok, trip_id, a, [a, b], "Piwo", "500", "CZK").get_data(as_text=True).replace("\xa0", " ")
    assert "500,00 Kč" in html and "86,00 zł" in html
    assert _rates(app, trip_id) == {"CZK": ("0.172", "NBP")}
    with app.app_context():
        assert db.session.get(Trip, trip_id).rate_for("CZK").rate_date == date(2026, 7, 1)
    nbp_calls = len([u for u in fake_rates if "nbp" in u])

    # kolejny rachunek w tej samej walucie: kurs już jest, NBP nie jest pytany ponownie
    FAKE_NBP["CZK"] = "0.2000"
    try:
        _manual_bill(client, tok, trip_id, b, [a, b], "Knedliki", "250", "CZK", day="2026-07-10")
        assert len([u for u in fake_rates if "nbp" in u]) == nbp_calls
        assert _rates(app, trip_id) == {"CZK": ("0.172", "NBP")}
        with app.app_context():
            s = summarize(db.session.get(Trip, trip_id))
            assert s.total == 8600 + 4300

        # kurs ręczny - dotyczy wszystkich rachunków w CZK
        r = client.post(f"/trips/{trip_id}/rates", data={"_csrf": tok, "rate_CZK": "0,18"}, follow_redirects=True)
        assert "Zapisano kurs" in r.get_data(as_text=True)
        assert _rates(app, trip_id) == {"CZK": ("0.18", "manual")}
        with app.app_context():
            assert summarize(db.session.get(Trip, trip_id)).total == 9000 + 4500

        # odśwież = najnowszy kurs NBP (nadpisuje ręczny)
        client.post(f"/trips/{trip_id}/rates/CZK/refresh", data={"_csrf": tok})
        assert _rates(app, trip_id) == {"CZK": ("0.2", "NBP")}
    finally:
        FAKE_NBP["CZK"] = "0.1720"

    page = client.get(f"/trips/{trip_id}/rates").get_data(as_text=True)
    assert 'name="rate_CZK"' in page and "2 rach." in page
    assert 'name="rate_CZK"' not in client.get(f"/trips/{trip_id}/settle").get_data(as_text=True)
    with app.app_context():
        bill_id = Bill.query.filter_by(trip_id=trip_id, title="Piwo").one().id
    assert client.get(f"/bills/{bill_id}").status_code == 200

    # zmiana waluty rachunku na walutę wyjazdu - kurs nie jest potrzebny
    client.post(f"/bills/{bill_id}/edit", data={"_csrf": tok, "payer_id": a, "title": "Piwo", "currency": "PLN"})
    with app.app_context():
        bill = db.session.get(Bill, bill_id)
        assert bill.currency == "PLN" and not bill.missing_rate


def test_refresh_rejects_external_redirect(app, client):
    tok, trip_id, (a, b) = _trip(client, app, "Redirect")
    _manual_bill(client, tok, trip_id, a, [a], "x", "1", "EUR")
    r = client.post(f"/trips/{trip_id}/rates/EUR/refresh", data={"_csrf": tok, "next": "//evil.example"})
    assert r.location.endswith(f"/trips/{trip_id}/rates")


def test_missing_rate_is_flagged_and_can_be_set(app, client):
    tok, trip_id, (a, b) = _trip(client, app, "Egzotyka")
    html = _manual_bill(client, tok, trip_id, a, [a, b], "Bazar", "100", "XYZ").get_data(as_text=True)
    assert "brak kursu" in html and "bez ustalonego kursu" in html
    with app.app_context():
        assert summarize(db.session.get(Trip, trip_id)).total == 0
    client.post(f"/trips/{trip_id}/rates", data={"_csrf": tok, "rate_XYZ": "0,5"})
    with app.app_context():
        assert summarize(db.session.get(Trip, trip_id)).total == 5000
    r = client.post(f"/trips/{trip_id}/rates", data={"_csrf": tok, "rate_XYZ": "abc"}, follow_redirects=True)
    assert "niepoprawny kurs" in r.get_data(as_text=True)


def test_ai_detects_currency(app, client, monkeypatch):
    tok, trip_id, (a, b) = _trip(client, app, "Wiedeń")

    def fake_parse(paths, cats, currency):
        return receipt_ai.normalize({
            "merchant": "Billa", "date": "2026-07-02", "currency": "€", "total": 12.5,
            "items": [{"name": "Wasser", "price": 2.5}, {"name": "Brot", "price": 10}],
        })

    monkeypatch.setattr(receipt_ai, "parse_receipt", fake_parse)
    buf = io.BytesIO()
    Image.new("RGB", (40, 80), "white").save(buf, "PNG")
    buf.seek(0)
    r = client.post(f"/trips/{trip_id}/bills/new", data={
        "_csrf": tok, "mode": "ai", "payer_id": a, "consumers": [a, b], "currency": "", "photos": (buf, "p.png")},
        content_type="multipart/form-data", follow_redirects=True)
    assert "1 EUR = 4,3 zł" in r.get_data(as_text=True).replace("\xa0", " ")
    assert _rates(app, trip_id) == {"EUR": ("4.3", "NBP")}
    with app.app_context():
        bill = Bill.query.filter_by(trip_id=trip_id).one()
        assert bill.currency == "EUR" and bill.total == 1250 and bill.total_mismatch is None
        assert summarize(bill.trip).total == 5375


def test_ai_respects_chosen_currency(app, client, monkeypatch):
    tok, trip_id, (a, b) = _trip(client, app, "Ręczna waluta")
    monkeypatch.setattr(receipt_ai, "parse_receipt", lambda *a_, **k: receipt_ai.normalize(
        {"merchant": "X", "currency": "EUR", "items": [{"name": "a", "price": 1}]}))
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    buf.seek(0)
    client.post(f"/trips/{trip_id}/bills/new", data={
        "_csrf": tok, "mode": "ai", "payer_id": a, "currency": "USD", "photos": (buf, "p.png")},
        content_type="multipart/form-data")
    with app.app_context():
        assert Bill.query.filter_by(trip_id=trip_id).one().currency == "USD"
    assert _rates(app, trip_id) == {"USD": ("3.9", "NBP")}


def test_trip_currency_change(app, client):
    tok, trip_id, (a, b) = _trip(client, app, "Zmiana")
    _manual_bill(client, tok, trip_id, a, [a, b], "PLN", "43", "PLN", day="2026-07-02")
    _manual_bill(client, tok, trip_id, a, [a, b], "CZK", "250", "CZK", day="2026-07-02")
    client.post(f"/trips/{trip_id}/edit", data={"_csrf": tok, "name": "Zmiana", "currency": "EUR",
                                               "participants": [a, b]})
    with app.app_context():
        t = db.session.get(Trip, trip_id)
        bills = {b_.title: b_ for b_ in t.bills}
        assert bills["PLN"].currency == "EUR" and not bills["PLN"].is_foreign  # waluta wyjazdu idzie za wyjazdem
        assert bills["CZK"].currency == "CZK"
        assert summarize(t).total == 4300 + 1000
    assert _rates(app, trip_id) == {"CZK": ("0.04", "NBP")}  # kurs wobec EUR, stary (wobec PLN) usunięty


def test_api_uses_bill_currency(app, client):
    tok, trip_id, (a, b) = _trip(client, app, "API")
    _manual_bill(client, tok, trip_id, a, [a, b], "Kawa", "10", "EUR", day="2026-07-02")
    with app.app_context():
        prod = Product.query.one()
        pid, bid = prod.id, prod.bill_id
    r = client.post(f"/api/products/{pid}", json={"price": "20"}, headers={"X-CSRF-Token": tok}).get_json()
    bind = {k: v.replace("\xa0", " ") for k, v in r["bind"].items()}
    assert bind[f"bill-total:{bid}"] == "20,00 €"
    assert bind[f"bill-converted:{bid}"] == "≈ 86,00 zł"
    assert bind[f"balance:{a}"] == "+43,00 zł"
    assert client.get(f"/trips/{trip_id}/export.csv").get_data(as_text=True).count("4,3") == 1


def test_upgrade_schema_moves_bill_rates_to_trip(tmp_path, monkeypatch):
    import sqlite3
    dbfile = tmp_path / "old.db"
    con = sqlite3.connect(dbfile)
    con.execute("CREATE TABLE trip (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, description TEXT,"
                " currency VARCHAR(3) NOT NULL, start_date DATE, end_date DATE, created_at DATETIME)")
    con.execute("CREATE TABLE bill (id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL, title VARCHAR(200) NOT NULL,"
                " category_id INTEGER, payer_id INTEGER NOT NULL, date DATE, note TEXT, declared_total INTEGER,"
                " source VARCHAR(10), created_at DATETIME, currency VARCHAR(3) NOT NULL DEFAULT 'PLN',"
                " exchange_rate VARCHAR(24), rate_date DATE, rate_source VARCHAR(10))")
    con.execute("INSERT INTO trip (id, name, currency) VALUES (1, 'Stary', 'PLN')")
    con.executemany("INSERT INTO bill (id, trip_id, title, payer_id, currency, exchange_rate, rate_date, rate_source)"
                    " VALUES (?, 1, 'x', 1, ?, ?, ?, ?)", [
                        (1, "EUR", "4.39", "2026-11-06", "NBP"),
                        (2, "EUR", "4.5", None, "manual"),
                        (3, "CZK", "0.17", "2026-11-06", "NBP"),
                        (4, "PLN", None, None, None),
                    ])
    con.commit()
    con.close()
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    for _ in range(2):  # drugi start niczego nie dubluje
        create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SQLALCHEMY_DATABASE_URI": f"sqlite:///{dbfile}"})
    con = sqlite3.connect(dbfile)
    rows = con.execute("SELECT currency, rate, source FROM trip_rate ORDER BY currency").fetchall()
    assert rows == [("CZK", "0.17", "NBP"), ("EUR", "4.5", "manual")]
    assert con.execute("SELECT COUNT(*) FROM bill WHERE exchange_rate IS NOT NULL").fetchone() == (0,)


def test_upgrade_schema_adds_bill_currency(tmp_path, monkeypatch):
    import sqlite3
    dbfile = tmp_path / "old.db"
    con = sqlite3.connect(dbfile)
    con.execute("CREATE TABLE trip (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, description TEXT,"
                " currency VARCHAR(3) NOT NULL, start_date DATE, end_date DATE, created_at DATETIME)")
    con.execute("CREATE TABLE bill (id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL, title VARCHAR(200) NOT NULL,"
                " category_id INTEGER, payer_id INTEGER NOT NULL, date DATE, note TEXT, declared_total INTEGER,"
                " source VARCHAR(10), created_at DATETIME)")
    con.execute("INSERT INTO trip (id, name, currency) VALUES (1, 'Stary', 'EUR')")
    con.execute("INSERT INTO bill (id, trip_id, title, payer_id) VALUES (1, 1, 'x', 1)")
    con.commit()
    con.close()
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SQLALCHEMY_DATABASE_URI": f"sqlite:///{dbfile}"})
    con = sqlite3.connect(dbfile)
    assert "currency" in [r[1] for r in con.execute("PRAGMA table_info(bill)")]
    assert con.execute("SELECT currency FROM bill WHERE id = 1").fetchone() == ("EUR",)
