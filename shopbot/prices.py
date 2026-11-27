"""Парсер цен BorzMarket. Работает отдельно от Telegram."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from card import ParseError, Product, fmt_price
from links import normalize_references, product_link
from ozon import fetch_ozon
from storefront import PageMarkup, from_page, node_matches, product_nodes, sku_in_url
from wb import fetch_wb
import bhapi
import net
import wbcon
from diagnostics import step
from storefront import merged


def market_url(value):
    market, url = product_link(normalize_references(value.strip()))
    return market, str(url)


# Полные ответы живут три минуты: повторная ссылка, добавление в подборку и «Дальше» не трогают площадку.
# Неполные не кэшируются никогда, чтобы «Загрузить недостающее» всегда делало настоящий запрос.
_RESULT_CACHE = net.TTLCache(180, 64)


def _complete(product) -> bool:
    return bool(product.title and product.price and product.image_urls and not product.price_unavailable)


async def fetch_product(value, *, direct_only=False, allow_paid=True, fresh=False):
    market, url = market_url(value)
    key = (market, url)
    result = None if fresh else _RESULT_CACHE.get(key)
    if result is not None:
        result = copy.deepcopy(result)
        step("Кэш", "ответ площадки до трёх минут назад")
    else:
        def load():
            return fetch_ozon(url) if market == "ozon" else fetch_wb(url)
        result = await load() if fresh else await net.once(key, load)
        if _complete(result):
            _RESULT_CACHE.set(key, copy.deepcopy(result))
    if direct_only:
        return result
    return (await supplement_products([result], allow_paid=allow_paid))[0]


async def supplement_wb(products):
    """Объединяет недостающие цены WB из одной подборки в запросы по пять артикулов."""
    output = list(products)
    if os.getenv("WB_PRICE_FALLBACK", "1") != "1":
        return output
    pending = [i for i, p in enumerate(output) if isinstance(p, Product) and p.marketplace == "wb"
               and p.price is None and wbcon.available(p)]
    for start in range(0, len(pending), 5):
        indexes = pending[start:start + 5]
        try:
            quotes = await wbcon.lookup_batch([output[i] for i in indexes])
        except ParseError as error:
            for i in indexes:
                output[i] = replace(output[i], import_note=(output[i].import_note + " " + str(error)).strip())
            continue
        for i, quote in zip(indexes, quotes):
            p = output[i]
            if quote.price is None:
                reason = quote.import_note or "WBCON не указал положительную цену."
                output[i] = replace(p, import_note=(p.import_note + " " + reason).strip())
            else:
                output[i] = replace(p, price=quote.price, price_note=quote.price_note, price_source="wbcon", used_fallback=True,
                                    import_note="Цена из WBCON. Сверьте её в приложении перед публикацией.")
    return output


async def supplement_products(products, *, allow_paid=True):
    """WB объединяется в пакет. Для Ozon демо принимает по одному артикулу."""
    output = await supplement_wb(products)
    for index, product in enumerate(output):
        if not isinstance(product, Product) or product.price_unavailable or not sku_in_url(product.url):
            continue
        providers = []
        if product.marketplace == "ozon" and os.getenv("OZON_PRICE_FALLBACK", "1") == "1":
            providers.append(("WBCON", wbcon.lookup))
        if allow_paid and bhapi.token(product.marketplace):
            providers.append(("BHAPI", bhapi.lookup))
        for label, lookup in providers:
            if product.title and product.price and product.image_urls:
                break
            try:
                extra = await lookup(product)
            except ParseError as error:
                step(label, str(error))
                product = replace(product, import_note=(product.import_note + " " + str(error)).strip())
                continue
            fields = [name for name, value in (("название", extra.title), ("цена", extra.price),
                                                ("галерея", extra.image_urls)) if value]
            step(label, ", ".join(fields) or "поля не получены")
            note = product.import_note
            previous = product
            product = merged(product, extra)
            product.used_fallback = previous.used_fallback or bool(
                product.title != previous.title or product.price != previous.price or product.image_urls != previous.image_urls
            )
            if not (product.title and product.price and product.image_urls):
                product = replace(product, import_note=" ".join(filter(None, (note, extra.import_note))))
            if not fields and not extra.import_note:
                product = replace(product, import_note=(product.import_note + f" {label} не вернул данные артикула.").strip())
        output[index] = product
    return output


def saved_page(value, path):
    return saved_markup(value, path.read_text(encoding="utf-8-sig"))


def saved_markup(value, markup):
    parser = PageMarkup()
    parser.feed(markup)
    parser.close()
    if not value:
        value = parser.canonical or parser.meta.get("og:url") or ""
        if not value:
            raise ParseError("В подписи к HTML укажите прямую ссылку или ozon:АРТИКУЛ / wb:АРТИКУЛ.")
    market, url = market_url(value)
    sku = sku_in_url(url)
    if not sku:
        raise ParseError("Для HTML нужна полная ссылка на товар или wb:АРТИКУЛ / ozon:АРТИКУЛ.")
    if market == "wb" and ("size" in httpx.URL(url).params or (os.getenv("WB_DEST") or "-1257786") != "-1257786"):
        raise ParseError("HTML не подтверждает цену выбранного размера или региона WB.")
    urls = [parser.canonical, parser.meta.get("og:url")]
    sku_match = any(node_matches(node, sku) is True for doc in parser.documents for node in product_nodes(doc))
    canonical_match = False
    for candidate in filter(None, urls):
        other_market, other_url = market_url(str(httpx.URL(url).join(candidate)))
        if other_market != market or sku_in_url(other_url) != sku:
            raise ParseError("В HTML указан адрес другого товара.")
        canonical_match = True
    if not (canonical_match or sku_match):
        raise ParseError("В HTML не удалось подтвердить артикул товара.")
    response = httpx.Response(200, text=markup, request=httpx.Request("GET", url))
    return from_page(Product(market, "", url), response)


def record(product, mode):
    def price(value):
        return format(value, "f") if value is not None else None

    return {
        "marketplace": product.marketplace, "sku": sku_in_url(product.url), "url": product.url,
        "title": product.title, "image_urls": product.image_urls, "price": price(product.price), "currency": "RUB",
        "old_price": price(product.old_price), "price_note": product.price_note or None,
        "conditional_price": price(product.conditional_price), "conditional_note": product.conditional_note or None,
        "source": product.price_source or None, "mode": mode,
        "requested_size": httpx.URL(product.url).params.get("size"),
        "requested_wb_dest": (os.getenv("WB_DEST") or "-1257786") if product.marketplace == "wb" and mode == "live" else None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok" if product.price is not None else ("unavailable" if product.price_unavailable else "unknown"),
        "message": product.import_note or None,
    }


async def run(args):
    slots = asyncio.Semaphore(3)

    async def collect(value):
        async with slots:
            try:
                if args.html:
                    return saved_page(value, args.html)
                return await fetch_product(value, direct_only=True)
            except (ParseError, OSError, UnicodeError, httpx.HTTPError, httpx.InvalidURL, asyncio.TimeoutError) as error:
                return {"input": value, "status": "error", "price": None, "message": str(error) or "Истекло время ожидания."}

    results = await asyncio.gather(*(collect(value) for value in args.products))
    if not args.html and not args.direct_only:
        results = await supplement_products(results)
    mode = "saved_html" if args.html else "live"
    rows = [record(p, mode) if isinstance(p, Product) else p for p in results]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(row.get("title") or row.get("url") or row.get("input"))
            if row["price"] is None:
                print(row.get("message") or "Цена не получена.")
                continue
            caption = fmt_price(Decimal(row["price"]))
            print(caption + (" · " + row["price_note"] if row["price_note"] else ""))
            if row["conditional_price"] and row["conditional_price"] != row["price"]:
                print(f"{row['conditional_note']} · {fmt_price(Decimal(row['conditional_price']))}")
            print(f"Источник: {row['source']}")
    return 0 if all(row["status"] == "ok" for row in rows) else 1


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("products", nargs="+", help="Ссылки, wb:АРТИКУЛ или ozon:АРТИКУЛ")
    parser.add_argument("--direct-only", action="store_true", help="Запрашивать только площадки")
    parser.add_argument("--json", action="store_true", help="Результаты в JSON")
    parser.add_argument("--html", type=Path, metavar="ФАЙЛ", help="Разобрать сохранённую страницу UTF-8 офлайн")
    args = parser.parse_args()
    if args.html and len(args.products) != 1:
        parser.error("--html используется для одного товара")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
