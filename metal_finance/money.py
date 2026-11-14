"""Kwoty trzymamy w groszach (int), żeby uniknąć błędów zaokrągleń na floatach."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction

CURRENCY_SYMBOLS = {"PLN": "zł", "EUR": "€", "USD": "$", "GBP": "£", "CZK": "Kč", "CHF": "CHF"}
CURRENCIES = ["PLN", "EUR", "USD", "GBP", "CZK", "CHF", "HUF", "NOK", "SEK", "DKK"]


def parse_money(value) -> int | None:
    """'12,50' / '12.50' / '1 234,5 zł' / 12.5  ->  1250 (grosze). Puste -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Niepoprawna kwota")
    if isinstance(value, (int, float, Decimal)):
        d = Decimal(str(value))
    else:
        s = str(value).strip()
        for junk in (" ", " ", "zł", "zl", "PLN", "€", "$", "EUR"):
            s = s.replace(junk, "")
        if not s:
            return None
        if "," in s and "." in s:
            # ostatni separator to separator dziesiętny
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        try:
            d = Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"Niepoprawna kwota: {value!r}") from exc
    if not d.is_finite():
        raise ValueError("Niepoprawna kwota")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_quantity(value) -> float:
    if value is None or str(value).strip() == "":
        return 1.0
    try:
        q = float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError as exc:
        raise ValueError(f"Niepoprawna ilość: {value!r}") from exc
    if q != q or q in (float("inf"), float("-inf")):
        raise ValueError("Niepoprawna ilość")
    return q


def round_cents(value: Fraction | int) -> int:
    """Zaokrąglenie ułamka groszy do pełnych groszy (połówki od zera)."""
    f = Fraction(value)
    sign = -1 if f < 0 else 1
    f = abs(f)
    whole = f.numerator // f.denominator
    if (f - whole) * 2 >= 1:
        whole += 1
    return sign * whole


def _group(int_part: str) -> str:
    out = []
    while len(int_part) > 3:
        out.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    out.insert(0, int_part)
    return " ".join(out)


def fmt_amount(cents: int | Fraction | None, currency: str | None = None, sign: bool = False) -> str:
    """1234567 -> '12 345,67 zł' (polski format)."""
    if cents is None:
        return ""
    cents = round_cents(cents) if isinstance(cents, Fraction) else int(cents)
    neg = cents < 0
    a = abs(cents)
    s = f"{_group(str(a // 100))},{a % 100:02d}"
    if neg:
        s = "−" + s
    elif sign and cents > 0:
        s = "+" + s
    if currency:
        s += " " + CURRENCY_SYMBOLS.get(currency, currency)
    return s


def fmt_input(cents: int | None) -> str:
    """Wartość do pola formularza: 1250 -> '12,50'."""
    if cents is None:
        return ""
    neg = cents < 0
    a = abs(int(cents))
    return f"{'-' if neg else ''}{a // 100},{a % 100:02d}"


def fmt_qty(q: float | None) -> str:
    if q is None:
        return ""
    if float(q).is_integer():
        return str(int(q))
    return f"{q:.3f}".rstrip("0").rstrip(".").replace(".", ",")
