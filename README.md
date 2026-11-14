# 🤘 Metal Finance

Aplikacja webowa (Flask) do rozliczania się ze znajomymi za wspólne wyjazdy i aktywności.

- **Wyjazd / rozliczenie** → składa się z **rachunków** → rachunek składa się z **pozycji (produktów)**.
- Każdy rachunek ma **płatnika**, każda pozycja ma **konsumentów** - koszt pozycji dzieli się po równo między nich.
- **Tabela** w stylu Excela: wiersze = pozycje, kolumny = osoby; klikasz, kto z czego korzystał, a sumy i salda przeliczają się od razu.
- **Kategorie** rachunków (paliwo, jedzenie, nocleg…) - własne, z ikoną i kolorem.
- Rachunki dodajesz **ręcznie** albo **zdjęciem paragonu** - pozycje odczytuje **Claude w ramach subskrypcji Pro/Max** (nie płatne API pay-as-you-go). Zdjęcia zostają przy rachunku.
- **Rozliczenie**: salda, minimalna lista przelewów „kto komu ile”, zapis spłat, podział wydatków na kategorie, tekst do wklejenia na grupę, eksport CSV do Excela.

## Postawienie aplikacji - jeden plik

Wymagany jest Docker z Compose v2 (BuildKit). Kod pobiera się z repozytorium jako kontekst budowania, więc na serwerze potrzebny jest tylko `docker-compose.yml`.

1. Na serwerze utwórz pusty katalog i pobierz do niego [`docker-compose.yml`](docker-compose.yml):

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/norwagov/metal-finance/main/docker-compose.yml
   ```

2. W pliku ustaw:
   - `CLAUDE_CODE_OAUTH_TOKEN` - token subskrypcji Claude (patrz niżej),
   - `APP_PASSWORD` - wspólne hasło ekipy (zalecane, jeśli aplikacja jest dostępna z internetu).

   Kod budowany jest z `https://github.com/norwagov/metal-finance.git#main` (pole `context:`); fork wystarczy podmienić tam.
3. Uruchom:

   ```bash
   docker compose up -d --build
   ```

4. Otwórz `http://<serwer>:8080`.

Aktualizacja do najnowszej wersji z repozytorium to to samo polecenie: `docker compose up -d --build`.
Dane (baza SQLite i zdjęcia) leżą w wolumenie `metal-finance-data` i przetrwają aktualizacje.

## Claude w ramach subskrypcji (nie API)

Odczyt paragonów działa przez **Claude Agent SDK** (silnik Claude Code, zawarty w paczce `claude-agent-sdk`), uwierzytelniony tokenem subskrypcji:

```bash
claude setup-token
```

Polecenie uruchom na komputerze z zainstalowanym Claude Code i zaloguj się kontem z planem **Pro lub Max**. Wypisany token (ważny rok) wklej do `CLAUDE_CODE_OAUTH_TOKEN`.

- Aplikacja **usuwa ze środowiska** `ANTHROPIC_API_KEY` i `ANTHROPIC_AUTH_TOKEN` (oraz przełączniki Bedrock/Vertex/Foundry) - w Claude Code mają one pierwszeństwo przed tokenem subskrypcji, więc nic nie trafi na rachunek płatnego API.
- Claude dostaje tylko zdjęcie i polecenie - bez dostępu do narzędzi, plików czy internetu.
- Zużycie wlicza się do limitów Twojej subskrypcji. Model ustawisz w `CLAUDE_MODEL` (`opus`, `sonnet`, `haiku` lub pełna nazwa).
- Token daje dostęp do Twojej subskrypcji - traktuj go jak hasło i używaj aplikacji prywatnie, w swojej ekipie (ustaw `APP_PASSWORD`). Udostępnianie logowania claude.ai w produktach dla osób trzecich wymaga zgody Anthropic.
- Status i test połączenia: strona **Ustawienia** w aplikacji.

Bez tokenu aplikacja działa normalnie - zdjęcie zapisze się przy rachunku, a pozycje wpiszesz ręcznie.

## Zmienne środowiskowe

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | - | token z `claude setup-token` (subskrypcja Pro/Max) |
| `CLAUDE_MODEL` | `opus` | model do odczytu paragonów |
| `CLAUDE_EFFORT` | - | opcjonalnie: `low` / `medium` / `high` |
| `CLAUDE_TIMEOUT` | `240` | limit czasu odczytu (sekundy) |
| `APP_PASSWORD` | - | wspólne hasło; puste = bez logowania |
| `TRUST_PROXY` | `0` | `1` za reverse proxy (nginx, Traefik, Caddy) |
| `MAX_UPLOAD_MB` | `60` | maks. rozmiar wysyłanych zdjęć na raz |
| `SECRET_KEY` | losowy | klucz sesji; domyślnie generowany i zapisywany w `/data/secret_key` |
| `DATA_DIR` | `/data` | katalog na bazę i zdjęcia |

## Jak liczone jest rozliczenie

- Płatnik rachunku „zapłacił” sumę jego pozycji.
- Koszt pozycji dzieli się po równo między zaznaczonych konsumentów (dokładnie, na ułamkach - zaokrąglenie do grosza dopiero na końcu, salda zawsze sumują się do zera).
- Po włączeniu **wag** w pozycji (przycisk z suwakami w wierszu) przy każdej osobie pojawia się waga (domyślnie 1) i koszt dzieli się proporcjonalnie - np. wagi 2 i 1 przy 30 zł dają 20 zł i 10 zł. Waga 0 = osoba zaznaczona, ale nie płaci. Wyłączenie wag wraca do podziału po równo (wagi zostają zapamiętane).
- Pozycja bez konsumentów **nie wchodzi** do rozliczenia (koszt zostaje u płacącego) - aplikacja wyraźnie ją oznacza.
- Saldo = zapłacone − zużyte ± spłaty. Plus = dostaje pieniądze, minus = oddaje.
- Przelewy: dłużnicy są parowani z wierzycielami tak, by przelewów było jak najmniej (maks. n−1).
- Rabaty z paragonu Claude odejmuje od pozycji, której dotyczą. Jeśli suma pozycji różni się od sumy z paragonu, rachunek dostaje ostrzeżenie.
- Kwoty w walucie wyjazdu; paragon w innej walucie jest oznaczany, ale nie przeliczany.

## Kopia zapasowa

```bash
docker run --rm -v metal-finance-data:/data -v "$PWD":/backup alpine tar czf /backup/metal-backup.tgz -C /data .
```

(Nazwa wolumenu może mieć prefiks katalogu projektu - sprawdź `docker volume ls`.)

## Rozwój lokalny

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/flask --app wsgi demo                    # przykładowy wyjazd
.venv/bin/flask --app wsgi run --debug             # http://127.0.0.1:5000
.venv/bin/python -m pytest                         # testy
docker compose -f docker-compose.dev.yml up --build  # obraz z lokalnego kodu
```

Struktura:

```
metal_finance/
  models.py        modele: Trip, Bill, Product, Person, Category, Payment, Attachment
  settlement.py    salda i minimalne przelewy
  receipt_ai.py    odczyt paragonów przez Claude (subskrypcja, Agent SDK)
  images.py        zapis zdjęć (EXIF, HEIC → JPEG, zmniejszanie)
  views/           trasy Flask: wyjazdy, rachunki, API tabeli, kategorie/ekipa/ustawienia
  templates/       Jinja2 + Bootstrap 5
  static/          CSS, JS (edycja w miejscu), logo
```
