"""Liczenie sald i minimalnej listy przelewów.

Zasady:
* płatnik rachunku "zapłacił" sumę wszystkich jego pozycji, które mają konsumentów,
* koszt pozycji dzielimy po równo między jej konsumentów (dokładnie, na ułamkach),
  a gdy pozycja ma włączone wagi - proporcjonalnie do wag konsumentów,
* pozycja bez konsumentów nie wchodzi do rozliczenia (koszt zostaje u płacącego),
* spłata A -> B zwiększa saldo A i zmniejsza saldo B,
* saldo > 0 oznacza, że osoba powinna dostać pieniądze.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from .money import round_cents


@dataclass
class PersonRow:
    person: object
    paid: int = 0
    consumed: Fraction = Fraction(0)
    sent: int = 0
    received: int = 0
    balance: int = 0  # w groszach, po zaokrągleniu

    @property
    def consumed_cents(self) -> int:
        return round_cents(self.consumed)


@dataclass
class Transfer:
    sender: object
    receiver: object
    amount: int


@dataclass
class Summary:
    people: list[PersonRow]
    transfers: list[Transfer]
    total: int  # suma wszystkich pozycji (także nieprzypisanych)
    settled_total: int  # suma pozycji z konsumentami
    unassigned: list = field(default_factory=list)  # Product bez konsumentów
    by_category: list = field(default_factory=list)  # [(category|None, {person_id: Fraction}, total_cents)]

    def row(self, person_id: int) -> PersonRow | None:
        for r in self.people:
            if r.person.id == person_id:
                return r
        return None


def product_shares(product) -> dict[int, Fraction]:
    cons = product.consumers
    if not cons:
        return {}
    if getattr(product, "weighted", False):
        weights = {c.id: Fraction(str(product.weight_of(c.id))) for c in cons}
        total = sum(weights.values())
        if total > 0:
            return {pid: Fraction(product.price) * w / total for pid, w in weights.items()}
    share = Fraction(product.price, len(cons))
    return {c.id: share for c in cons}


def bill_shares(bill) -> dict[int, Fraction]:
    out: dict[int, Fraction] = defaultdict(Fraction)
    for p in bill.products:
        for pid, s in product_shares(p).items():
            out[pid] += s
    return dict(out)


def summarize(trip) -> Summary:
    rows: dict[int, PersonRow] = {}
    order: list[int] = []

    def row(person) -> PersonRow:
        if person.id not in rows:
            rows[person.id] = PersonRow(person=person)
            order.append(person.id)
        return rows[person.id]

    for p in trip.participants:
        row(p)

    total = 0
    settled = 0
    unassigned = []
    cats: dict[object, dict] = {}

    for bill in trip.bills:
        cat_key = bill.category.id if bill.category else None
        if cat_key not in cats:
            cats[cat_key] = {"category": bill.category, "shares": defaultdict(Fraction), "total": 0}
        for prod in bill.products:
            total += prod.price
            shares = product_shares(prod)
            if not shares:
                unassigned.append(prod)
                continue
            settled += prod.price
            row(bill.payer).paid += prod.price
            cats[cat_key]["total"] += prod.price
            for c in prod.consumers:
                row(c).consumed += shares[c.id]
                cats[cat_key]["shares"][c.id] += shares[c.id]

    for pay in trip.payments:
        row(pay.sender).sent += pay.amount
        row(pay.receiver).received += pay.amount

    people = [rows[i] for i in order]
    exact = {r.person.id: Fraction(r.paid) - r.consumed + r.sent - r.received for r in people}
    rounded = {pid: round_cents(v) for pid, v in exact.items()}
    drift = sum(rounded.values())
    if drift and rounded:
        # korekta groszowych różnic z zaokrągleń: zabieramy/dokładamy osobie z największym saldem
        pid = max(rounded, key=lambda k: (abs(rounded[k]), k))
        rounded[pid] -= drift
    for r in people:
        r.balance = rounded[r.person.id]

    by_category = sorted(
        ((v["category"], dict(v["shares"]), v["total"]) for v in cats.values() if v["total"]),
        key=lambda t: -t[2],
    )

    return Summary(
        people=people,
        transfers=minimize_transfers(people),
        total=total,
        settled_total=settled,
        unassigned=unassigned,
        by_category=by_category,
    )


def minimize_transfers(people: list[PersonRow]) -> list[Transfer]:
    """Zachłanne parowanie największego dłużnika z największym wierzycielem.

    Daje co najwyżej n-1 przelewów i w praktyce zwykle optimum.
    """
    creditors = [[r.balance, r.person] for r in people if r.balance > 0]
    debtors = [[-r.balance, r.person] for r in people if r.balance < 0]
    transfers: list[Transfer] = []
    while creditors and debtors:
        creditors.sort(key=lambda x: -x[0])
        debtors.sort(key=lambda x: -x[0])
        c, d = creditors[0], debtors[0]
        amount = min(c[0], d[0])
        transfers.append(Transfer(sender=d[1], receiver=c[1], amount=amount))
        c[0] -= amount
        d[0] -= amount
        if c[0] == 0:
            creditors.pop(0)
        if d[0] == 0:
            debtors.pop(0)
    return transfers
