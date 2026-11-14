"""Polecenia `flask ...` (np. dane demo: `flask --app wsgi demo`)."""
from __future__ import annotations

from datetime import date, timedelta

import click

from .models import Bill, Category, Person, Product, Trip, db


def register(app) -> None:
    @app.cli.command("demo")
    def demo():
        """Tworzy przykładowy wyjazd z rachunkami."""
        names = ["Ania", "Bartek", "Czarek", "Dominika"]
        people = []
        for n in names:
            p = Person.query.filter_by(name=n).first() or Person(name=n)
            db.session.add(p)
            people.append(p)
        a, b, c, d = people
        cat = {x.name: x for x in Category.query.all()}
        start = date.today() - timedelta(days=10)
        trip = Trip(name="Festiwal metalowy 2026 (demo)", description="Przykładowe dane do zabawy.",
                    currency="PLN", start_date=start, end_date=start + timedelta(days=3), participants=people)
        db.session.add(trip)

        def bill(title, category, payer, day, items):
            bl = Bill(trip=trip, title=title, category=cat.get(category), payer=payer, date=start + timedelta(days=day))
            for i, (name, qty, price, cons) in enumerate(items):
                bl.products.append(Product(name=name, quantity=qty, price=price, position=i, consumers=cons))
            db.session.add(bl)

        bill("Orlen", "Paliwo", a, 0, [("Pb95", 42.5, 27413, people)])
        bill("Biedronka", "Zakupy", b, 0, [
            ("Kiełbasa śląska", 2, 2598, [a, b, c]),
            ("Bułki", 10, 650, people),
            ("Woda 6x1,5l", 2, 1798, people),
            ("Chipsy", 3, 1497, [b, d]),
        ])
        bill("Piwo na polu namiotowym", "Alkohol", c, 1, [
            ("Piwo 0,5l", 8, 9600, [a, b, c]),
            ("Lemoniada", 2, 1800, [d]),
        ])
        bill("Pensjonat Pod Lasem", "Nocleg", d, 1, [("Pokój 4-os., 2 noce", 2, 64000, people)])
        bill("Bilety festiwalowe", "Bilety i atrakcje", a, 2, [("Karnet", 4, 159600, people)])
        db.session.commit()
        click.echo(f"Utworzono wyjazd demo (id={trip.id}).")
