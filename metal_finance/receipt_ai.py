"""Odczyt paragonów przez Claude w ramach SUBSKRYPCJI (Pro / Max), a nie API pay-as-you-go.

Działa przez Claude Agent SDK (silnik Claude Code, binarka jest w paczce pip). Uwierzytelnienie:
zmienna CLAUDE_CODE_OAUTH_TOKEN z tokenem wygenerowanym poleceniem `claude setup-token`.

Zmienne ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN mają w Claude Code pierwszeństwo przed tokenem
subskrypcji i przełączyłyby rozliczanie na płatne API - dlatego usuwamy je ze środowiska procesu.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
)

from .money import parse_money, parse_quantity

log = logging.getLogger(__name__)

# Wszystko, co mogłoby przełączyć Claude Code z subskrypcji na inne (płatne) źródło.
_NON_SUBSCRIPTION_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "ANTHROPIC_PROFILE",
)


class ReceiptError(Exception):
    """Błąd czytelny dla użytkownika (po polsku)."""


def _scrub_env() -> None:
    for key in _NON_SUBSCRIPTION_ENV:
        if os.environ.pop(key, None) is not None:
            log.warning("Usunięto %s ze środowiska - Metal Finance używa wyłącznie subskrypcji Claude.", key)


_scrub_env()


def model_name() -> str:
    return os.environ.get("CLAUDE_MODEL", "").strip() or "opus"


def is_configured() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip())


SYSTEM_PROMPT = (
    "You are a meticulous receipt and invoice parser for a cost-splitting app used by a group of "
    "friends. Documents are mostly Polish: paragony fiskalne, faktury, restaurant bills, fuel "
    "station, supermarket and hotel receipts. You receive one or more photos of the SAME document "
    "(a long receipt may be photographed in parts - do not double count overlapping lines). "
    "You read carefully, never invent lines that are not printed, and return the result through "
    "the structured output."
)

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string", "description": "Short store / venue name"},
        "date": {"type": ["string", "null"], "description": "Purchase date YYYY-MM-DD"},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code, e.g. PLN"},
        "total": {"type": ["number", "null"], "description": "Final amount to pay"},
        "category": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "number"},
                    "price": {"type": "number", "description": "Final price of the whole line"},
                },
                "required": ["name", "price"],
            },
        },
        "notes": {"type": ["string", "null"]},
    },
    "required": ["merchant", "items"],
}


def _receipt_prompt(categories: list[str], currency: str) -> str:
    cats = ", ".join(f'"{c}"' for c in categories) or "(none)"
    return f"""Read the document in the attached photo(s) and return:
- merchant: short store/venue name as printed (e.g. "Biedronka", "Orlen", "Pizzeria Roma"), at most ~40 characters.
- date: purchase date as YYYY-MM-DD, or null if not legible.
- currency: ISO code of the document currency ("zł" means PLN).
- total: the final amount to pay printed on the document (SUMA / DO ZAPŁATY / RAZEM / TOTAL), or null.
- category: the best matching name from this list, copied exactly, or null: {cats}
- items: every purchased line, in printed order, each with:
  - name: the product name as printed, cleaned up to be readable (no PLU codes, no VAT letters like "A"/"B"; keep the language of the document),
  - quantity: number of units / kg / litres (1 if not shown),
  - price: the FINAL amount paid for the whole line (quantity x unit price minus any discount on that line), as a number with a dot as decimal separator.

Rules:
- A discount line (RABAT, OPUST, PROMOCJA, obniżka, a negative amount) belongs to the item directly above it: subtract it from that item's price and do not list it separately. A discount for the whole document that cannot be attributed to one line becomes its own item with a negative price.
- Include bottle deposits (kaucja), service charges and tips as their own items. Skip VAT/PTU summaries, payment method lines, change, loyalty points and subtotal/total lines.
- Fuel receipts: one item per fuel line, e.g. "Pb95" or "ON (diesel)", quantity = litres.
- The item prices must add up to `total`. If they do not, re-read the document. If it still does not add up, keep what is printed and explain the difference in `notes`.
- If the image is not a receipt/invoice or is unreadable, return an empty items list and explain why in `notes`.
- notes: short remarks for the user written in Polish, or null.

The group's default currency is {currency}."""


def parse_receipt(image_paths: list[str], categories: list[str], currency: str = "PLN") -> dict:
    """Wysyła zdjęcia do Claude i zwraca znormalizowany słownik z danymi paragonu."""
    if not image_paths:
        raise ReceiptError("Brak zdjęć do odczytania.")
    content: list[dict] = []
    for i, path in enumerate(image_paths, 1):
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode()
        if len(image_paths) > 1:
            content.append({"type": "text", "text": f"Photo {i} of {len(image_paths)}:"})
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}
        )
    content.append({"type": "text", "text": _receipt_prompt(categories, currency)})
    raw = _run(_ask(content, schema=RECEIPT_SCHEMA, max_turns=6))
    if not isinstance(raw, dict):
        raise ReceiptError("Claude nie zwrócił danych w oczekiwanym formacie.")
    return normalize(raw)


def ping() -> str:
    """Krótki test połączenia z Claude (zużywa minimalnie limit subskrypcji)."""
    text = _run(_ask([{"type": "text", "text": "Odpowiedz dokładnie jednym słowem: DZIAŁA"}], max_turns=1))
    return str(text).strip()


def _run(coro):
    if not is_configured():
        coro.close()
        raise ReceiptError(
            "Brak tokenu subskrypcji Claude. Wygeneruj go poleceniem `claude setup-token` "
            "i ustaw jako CLAUDE_CODE_OAUTH_TOKEN w docker-compose.yml."
        )
    timeout = float(os.environ.get("CLAUDE_TIMEOUT", "240"))
    try:
        return asyncio.run(asyncio.wait_for(coro, timeout))
    except ReceiptError:
        raise
    except asyncio.TimeoutError as exc:
        raise ReceiptError(f"Claude nie odpowiedział w ciągu {int(timeout)} s. Spróbuj ponownie.") from exc
    except CLINotFoundError as exc:
        raise ReceiptError("Nie znaleziono silnika Claude Code w kontenerze (claude-agent-sdk).") from exc
    except ClaudeSDKError as exc:
        log.exception("Błąd Claude Agent SDK")
        raise ReceiptError(_friendly(str(exc))) from exc


async def _ask(content: list[dict], schema: dict | None = None, max_turns: int = 4):
    _scrub_env()

    async def messages():
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    effort = os.environ.get("CLAUDE_EFFORT", "").strip() or None
    texts: list[str] = []
    result: ResultMessage | None = None
    with tempfile.TemporaryDirectory(prefix="metal-claude-") as workdir:
        options = ClaudeAgentOptions(
            model=model_name(),
            system_prompt=SYSTEM_PROMPT,
            tools=[],  # żadnych narzędzi (pliki, bash, web) - tylko odczyt obrazu
            allowed_tools=[],
            setting_sources=[],  # nie wczytuj żadnych plików ustawień Claude Code
            max_turns=max_turns,
            cwd=workdir,
            effort=effort,
            output_format={"type": "json_schema", "schema": schema} if schema else None,
            env={
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            stderr=lambda line: log.debug("claude: %s", line),
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(messages())
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                elif isinstance(msg, ResultMessage):
                    result = msg

    if result is None:
        raise ReceiptError("Brak odpowiedzi od Claude.")
    if result.is_error or result.subtype != "success":
        detail = "; ".join(result.errors or []) or (result.result or "") or result.subtype
        raise ReceiptError(_friendly(detail))
    if schema is None:
        return result.result or "\n".join(texts)
    if result.structured_output is not None:
        return result.structured_output
    return _extract_json(result.result or "\n".join(texts))


def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ReceiptError("Claude nie zwrócił danych paragonu.")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReceiptError("Nie udało się odczytać odpowiedzi Claude (niepoprawny JSON).") from exc


def _friendly(detail: str) -> str:
    low = detail.lower()
    if any(k in low for k in ("not logged in", "invalid api key", "oauth", "401", "authentication", "unauthorized")):
        return (
            "Token subskrypcji Claude jest nieprawidłowy lub wygasł. Wygeneruj nowy poleceniem "
            "`claude setup-token` i podmień CLAUDE_CODE_OAUTH_TOKEN."
        )
    if any(k in low for k in ("usage limit", "rate limit", "429", "limit reached", "overloaded")):
        return "Osiągnięto limit subskrypcji Claude albo serwery są przeciążone. Spróbuj za jakiś czas."
    detail = detail.strip() or "nieznany błąd"
    return f"Claude zwrócił błąd: {detail[:400]}"


def _safe_money(value) -> int | None:
    try:
        return parse_money(value)
    except (ValueError, TypeError):
        return None


def _parse_date(value) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(str(value).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def normalize(data: dict) -> dict:
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        price = _safe_money(it.get("price"))
        if price is None:
            continue
        try:
            qty = parse_quantity(it.get("quantity"))
        except ValueError:
            qty = 1.0
        name = " ".join(str(it.get("name") or "").split())[:200] or "Pozycja"
        items.append({"name": name, "quantity": qty, "price": price})
    currency = str(data.get("currency") or "").strip().upper()[:3] or None
    return {
        "merchant": " ".join(str(data.get("merchant") or "").split())[:200],
        "date": _parse_date(data.get("date")),
        "currency": currency,
        "total": _safe_money(data.get("total")),
        "category": (str(data.get("category")).strip() if data.get("category") else None),
        "items": items,
        "notes": (str(data.get("notes")).strip() if data.get("notes") else None),
    }
