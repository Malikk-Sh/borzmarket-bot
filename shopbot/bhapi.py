"""Дополнительный источник карточек по подписке администратора."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace

import httpx

from card import ParseError, Product, parse_price
from diagnostics import step
from storefront import as_list, images, in_rubles, sku_in_url

API = "https://bhapi.ru"
_cooldown = {"wb": 0.0, "ozon": 0.0}


def token(market):
    return os.getenv(f"BHAPI_{market.upper()}_TOKEN", "").strip()


def parse(product, payload):
    market = product.marketplace
    sku = sku_in_url(product.url)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ParseError("BHAPI вернул ответ без карточки.")
    row = payload.get("data")
    if market == "wb" and isinstance(row, dict):
        row = row.get("data")
    if not isinstance(row, dict) or str(row.get("item_id" if market == "wb" else "sku")) != sku:
        raise ParseError("BHAPI не подтвердил запрошенный артикул.")
    title = row.get("title")
    base = Product(market, title.strip() if isinstance(title, str) else "", product.url)
    if market == "ozon":
        base.image_urls = images(base, as_list(row.get("images")))
        prices = row.get("prices")
        if not isinstance(prices, dict) or not in_rubles(prices.get("currency")):
            return base
        regular, card = parse_price(prices.get("regular")), parse_price(prices.get("bank_card"))
        note = "по данным BHAPI" if regular else "с банковской картой · BHAPI"
        old = parse_price(prices.get("original"))
        amount = regular or card
        if card:
            base.conditional_price, base.conditional_note = card, "с банковской картой"
    else:
        base.image_urls = images(base, as_list(row.get("main_imgs")) + as_list(row.get("additional_imgs")))
        fresh = row.get("data_freshness")
        dest = str(fresh.get("dest", "")) if isinstance(fresh, dict) else ""
        base.wb_dest = dest
        requested = os.getenv("WB_DEST") or "-1257786"
        if not in_rubles(row.get("currency")):
            return base
        # Для нестандартного региона нужен точный ответ по этому региону.
        if dest != requested and requested != "-1257786":
            base.import_note = "BHAPI не подтвердил выбранный регион WB."
            return base
        size = httpx.URL(product.url).params.get("size")
        if size:
            row = next((s for s in as_list(row.get("sizes")) if isinstance(s, dict)
                        and str(s.get("option_id")) == size), {})
        if row.get("availability_status") == "out_of_stock" or row.get("availability") == 0:
            return base
        prices = row.get("price_info")
        if not isinstance(prices, dict):
            return base
        amount, old = parse_price(prices.get("pre_sale_price")), parse_price(prices.get("origin_price"))
        # Общая цена для предпросмотра явно подписана регионом источника.
        note = "по данным BHAPI" if dest == requested else "регион сервиса BHAPI"
        if not size and row.get("price_size_option_id"):
            note += f" · вариант {str(row['price_size_option_id'])[:16]}"
    if amount is None:
        return base
    return replace(base, price=amount, old_price=old if old and old > amount else None,
                   price_note=note, price_source="bhapi")


async def lookup(product):
    market = product.marketplace
    sku = sku_in_url(product.url)
    if not sku or not token(market):
        raise ParseError("Для BHAPI нужны артикул и ключ площадки.")
    if time.monotonic() < _cooldown[market]:
        raise ParseError("Пауза BHAPI после ограничения запросов.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(32, connect=6), follow_redirects=False,
                                     headers={"X-API-Token": token(market)}) as client:
            if market == "ozon":
                request = client.post(API + "/ozon/api/v2/item/detail-by-sku", json={"sku": sku})
            else:
                url = f"https://www.wildberries.ru/catalog/{sku}/detail.aspx"
                request = client.get(API + "/wb/api/v1/item/by-url", params={"url": url})
            response = await asyncio.wait_for(request, 35)
    except (asyncio.TimeoutError, httpx.HTTPError, ImportError, ValueError) as error:
        raise ParseError(f"BHAPI · {type(error).__name__}") from error
    step("BHAPI", f"HTTP {response.status_code}")
    if response.status_code == 429:
        delay = response.headers.get("Retry-After", "")
        seconds = int(delay) if delay.isascii() and delay.isdigit() and len(delay) <= 6 else 60
        _cooldown[market] = time.monotonic() + max(1, seconds)
    if response.status_code != 200:
        raise ParseError(f"BHAPI · HTTP {response.status_code}")
    try:
        return parse(product, response.json())
    except ValueError as error:
        raise ParseError("BHAPI вернул ответ без JSON.") from error
