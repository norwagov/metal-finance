"""Kursy walut z darmowych, publicznych API (bez kluczy).

1. NBP (api.nbp.pl) - średnie kursy Narodowego Banku Polskiego: tabela A (główne waluty, codziennie)
   i tabela B (pozostałe, raz w tygodniu). Kursy są wobec PLN, więc parę bez PLN (np. CZK -> EUR)
   liczymy krzyżowo: mid(CZK) / mid(EUR).
2. Frankfurter (api.frankfurter.dev) - kursy referencyjne EBC, jako zapas, gdy NBP nie odpowiada.

Zawsze bierzemy ostatni kurs opublikowany w dniu rachunku lub przed nim (weekendy i święta).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

log = logging.getLogger(__name__)

NBP_URL = "https://api.nbp.pl/api/exchangerates/rates/{table}/{code}/{start}/{end}/?format=json"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/{day}?base={base}&symbols={symbol}"
TIMEOUT = float(os.environ.get("FX_TIMEOUT", "8"))
RATE_PLACES = Decimal("1e-10")  # 10 miejsc po przecinku - wystarcza też dla VND/IDR (0,000152…)
LOOKBACK_DAYS = 14  # tabela B wychodzi raz w tygodniu, do tego święta


class RateError(Exception):
    """Nie udało się pobrać kursu (komunikat po polsku)."""


@dataclass(frozen=True)
class Rate:
    value: Decimal  # ile jednostek waluty docelowej za 1 jednostkę waluty źródłowej
    day: date  # data publikacji kursu
    source: str  # "NBP" / "EBC"


_cache: dict[tuple, tuple[Decimal, date] | None] = {}
_lock = threading.Lock()


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "metal-finance"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode(), parse_float=Decimal)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None  # brak kursu dla tej waluty/okresu
        raise RateError(f"serwer kursów zwrócił błąd {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RateError("brak połączenia z serwerem kursów") from exc


def _cached(key: tuple, fetch):
    with _lock:
        if key in _cache:
            return _cache[key]
    value = fetch()
    with _lock:
        _cache[key] = value
    return value


def _nbp_mid(code: str, day: date) -> tuple[Decimal, date] | None:
    """Średni kurs NBP waluty w PLN z dnia `day` lub ostatniego wcześniejszego notowania."""
    if code == "PLN":
        return Decimal(1), day
    end = min(day, date.today())
    start = end - timedelta(days=LOOKBACK_DAYS)

    def fetch():
        for table in ("A", "B"):
            data = _get_json(NBP_URL.format(table=table, code=code.lower(), start=start, end=end))
            rates = (data or {}).get("rates") or []
            if rates:
                last = max(rates, key=lambda r: r["effectiveDate"])
                return Decimal(last["mid"]), date.fromisoformat(last["effectiveDate"])
        return None

    return _cached(("nbp", code, end), fetch)


def _from_nbp(src: str, dst: str, day: date) -> Rate | None:
    a, b = _nbp_mid(src, day), _nbp_mid(dst, day)
    if not a or not b:
        return None
    if src == "PLN":
        published = b[1]
    elif dst == "PLN":
        published = a[1]
    else:
        published = max(a[1], b[1])
    return Rate(value=_quantize(Fraction(a[0]) / Fraction(b[0])), day=published, source="NBP")


def _from_frankfurter(src: str, dst: str, day: date) -> Rate | None:
    end = min(day, date.today())

    def fetch():
        data = _get_json(FRANKFURTER_URL.format(day=end.isoformat(), base=src, symbol=dst))
        val = ((data or {}).get("rates") or {}).get(dst)
        if val is None:
            return None
        return Decimal(val), date.fromisoformat(data["date"])

    got = _cached(("ecb", src, dst, end), fetch)
    if not got:
        return None
    return Rate(value=_quantize(Fraction(got[0])), day=got[1], source="EBC")


def _quantize(value: Fraction) -> Decimal:
    d = Decimal(value.numerator) / Decimal(value.denominator)
    return d.quantize(RATE_PLACES, rounding=ROUND_HALF_UP).normalize()


def get_rate(src: str, dst: str, day: date | None = None) -> Rate:
    """Kurs src -> dst (1 src = X dst) z dnia `day` (domyślnie dziś)."""
    src, dst = src.upper(), dst.upper()
    day = day or date.today()
    if src == dst:
        return Rate(value=Decimal(1), day=day, source="")
    errors = []
    for provider in (_from_nbp, _from_frankfurter):
        try:
            rate = provider(src, dst, day)
        except RateError as exc:
            errors.append(str(exc))
            log.warning("Kurs %s->%s (%s): %s", src, dst, provider.__name__, exc)
            continue
        if rate:
            return rate
    if errors:
        raise RateError(f"Nie udało się pobrać kursu {src}/{dst}: {errors[-1]}.")
    raise RateError(f"Brak kursu {src}/{dst} w NBP ani w EBC - wpisz kurs ręcznie.")


def clear_cache() -> None:
    with _lock:
        _cache.clear()
