"""Правила выбора цены из виджета Ozon и ответа витрины WB."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from card import Product, parse_price


def kopecks(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    digits = str(value)
    if not digits.isascii() or not digits.isdigit() or len(digits) > 16:
        return None
    return Decimal(digits).scaleb(-2) if int(digits) else None


def widget_text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return widget_text(value.get("text") or value.get("title") or value.get("textRs"))
    if isinstance(value, list):
        return "".join(run["content"] for run in value if isinstance(run, dict)
                       and isinstance(run.get("content"), str)).strip()
    return ""


def widget_amount(value):
    if isinstance(value, dict):
        currency = value.get("currencyCode") or value.get("currency") or "RUB"
        if str(currency).upper() not in ("RUB", "₽", "RUR"):
            return None
        for key in ("price", "value", "text"):
            amount = parse_price(value.get(key))
            if amount:
                return amount
        value = widget_text(value)
    return parse_price(value)


def ozon_quote(base, widget):
    if widget.get("isAvailable") is False:
        return replace(base, price_unavailable=True, price_source="ozon.webPrice")
    regular = widget_amount(widget.get("priceWithoutCard")) or widget_amount(widget.get("price"))
    special = widget_amount(widget.get("cardPrice"))
    if not (regular or special):
        return base
    lexemes = widget.get("lexemes")
    label = widget_text(lexemes.get("withOzonCard")) if isinstance(lexemes, dict) else ""
    label = label[:80] or "с Ozon Картой"
    old = widget_amount(widget.get("originalPrice")) if widget.get("showOriginalPrice") is not False else None
    return replace(base, price=regular or special, price_note="" if regular else label,
                   old_price=old if regular and old and old > regular else None,
                   conditional_price=special, conditional_note=label if special else "",
                   price_source="ozon.webPrice")


def wb_amounts(row):
    prices = row.get("price")
    if isinstance(prices, dict):
        current = kopecks(prices.get("product")) or kopecks(prices.get("total"))
        if current:
            return current, kopecks(prices.get("basic"))
    return kopecks(row.get("salePriceU")), kopecks(row.get("priceU"))


def stock_status(size):
    stocks = size.get("stocks")
    if not isinstance(stocks, list):
        return None
    quantities = [s.get("qty") for s in stocks if isinstance(s, dict)]
    if any(type(q) is int and q > 0 for q in quantities):
        return True
    if len(quantities) == len(stocks) and all(type(q) is int and q == 0 for q in quantities):
        return False
    return None


def wb_quote(base, product, size_id=None):
    sizes = product.get("sizes")
    choices = [s for s in sizes if isinstance(s, dict)] if isinstance(sizes, list) else []
    single_variant = len(choices) == 1
    if size_id is not None:
        choices = [s for s in choices if str(s.get("optionId")) == str(size_id)]
        if not choices:
            return base
    known_stock = [s for s in choices if stock_status(s) is True]
    eligible = known_stock or [s for s in choices if stock_status(s) is None]
    if (choices and not eligible) or (product.get("totalQuantity") == 0 and not known_stock):
        return replace(base, price_unavailable=True, price_source="wb.detail")
    rows = [(wb_amounts(size), size) for size in eligible]
    priced = [(pair, size) for pair, size in rows if pair[0] is not None]
    note = ""
    if priced:
        (current, old), selected = min(priced, key=lambda record: record[0][0])
        if size_id is None and len(priced) != len(rows):
            label = selected.get("name") or selected.get("origName") or selected.get("optionId")
            if len(rows) > 1 and not label:
                return base
            note = f"вариант {str(label)[:40]}" if len(rows) > 1 else ""
        elif size_id is None and len({pair[0] for pair, _ in priced}) > 1:
            note = "минимум по доступным вариантам" if known_stock else "минимум по вариантам"
    elif (size_id is None and not choices) or (single_variant and eligible):
        current, old = wb_amounts(product)
    else:
        return base
    if current is None:
        return base
    return replace(base, price=current, old_price=old if old and old > current else None,
                   price_note=note, price_source="wb.detail")


def shared_quote(product, text):
    """Цена из подписанных строк, скопированных с карточки в Telegram."""
    regular_labels = {"цена", "обычная цена", "без wb кошелька"} if product.marketplace == "wb" else {
        "цена", "обычная цена", "без ozon карты", "с другими банками"}
    special_labels = ({"с wb кошельком", "с вб кошельком"} if product.marketplace == "wb" else
                      {"с банками", "с ozon картой"})
    primary = set()
    specials = {}
    for line in text.splitlines():
        line = " ".join(line.split()).strip()
        # Разрешаем только отдельную строку с одной суммой и известным условием.
        folded = line.casefold().replace("ё", "е")
        for label in regular_labels | special_labels:
            number = None
            if folded.startswith(label + ":") or folded.startswith(label + " "):
                number = folded[len(label):].lstrip(": ")
            elif folded.endswith(" " + label):
                number = folded[:-len(label)].strip()
            if number is None:
                continue
            if amount := parse_price(number):
                if label in regular_labels:
                    primary.add(amount)
                else:
                    label_text = {"с банками": "С банками", "с ozon картой": "с Ozon Картой"}
                    specials[amount] = label_text.get(label, "с WB Кошельком")
    if len(primary) > 1 or not primary and len(specials) != 1:
        return product
    regular = next(iter(primary), None)
    card, label = next(iter(specials.items())) if len(specials) == 1 else (None, "")
    return replace(product, price=regular or card, old_price=None, price_source="message",
                   price_note="" if regular else label, price_unavailable=False,
                   conditional_price=card, conditional_note=label)
