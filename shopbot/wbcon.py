"""Резервный источник WB и ручная проверка Ozon через публичные методы WBCON."""
from __future__ import annotations

import asyncio
import math
import os
import time
from collections import OrderedDict, deque
from dataclasses import replace
from decimal import Decimal

import httpx

from diagnostics import step
from card import ParseError, Product, parse_price
from storefront import images, sku_in_url

# У каждого демо-метода свой лимит. Проверка и резервирование выполняются до первого await.
_recent = {"wb": deque(), "ozon": deque()}
_blocked_until = {"wb": 0.0, "ozon": 0.0}


# Один процесс бота. Храним успешные ответы, чтобы открытие карточки после /diag не тратило запрос.
_cache = OrderedDict()
_cache_ttl = 300
_locks = {market: asyncio.Lock() for market in ("ozon", "wb")}


def available(product: Product) -> bool:
    if product.price_unavailable:
        return False
    sku = sku_in_url(httpx.URL(product.url).path)
    if not (sku and len(sku) <= 16):
        return False
    if product.marketplace == "ozon":
        return True
    return (product.marketplace == "wb" and "size" not in httpx.URL(product.url).params
            and (os.getenv("WB_DEST") or "-1257786") == "-1257786")


def reserve(market):
    now = time.monotonic()
    window = _recent[market]
    while window and window[0] <= now-60:
        window.popleft()
    deadline = max(_blocked_until[market], window[0]+60 if len(window) >= 2 else 0)
    if deadline > now:
        raise ParseError(f"Лимит WBCON. Попробуйте через {math.ceil(deadline-now)} с.")
    window.append(now)


def from_payload(product, sku, payload):
    result = Product(product.marketplace, "", product.url)
    if product.marketplace == "wb":
        rows = payload if isinstance(payload,list) else []
        rows = [r for r in rows if isinstance(r,dict) and str(r.get("id")) == sku]
        if len(rows) == 1:
            value = str(rows[0].get("salePriceU",""))
            if value.isascii() and value.isdigit() and len(value) <= 16 and int(value) > 0:
                result.price = Decimal(value).scaleb(-2)
    elif isinstance(payload,dict) and str(payload.get("article")) == sku:
        title = payload.get("name")
        result.title = title.strip() if isinstance(title,str) else ""
        result.image_urls = images(product,[payload.get("imageUrl")])
        result.price = parse_price(payload.get("price"))
    if result.price:
        result.price_note = "по данным WBCON"
        result.price_source = "wbcon"
    return result


async def request_prices(market, articles):
    reserve(market)
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            if market == "wb":
                r = await client.get("https://prices.wbcon.su/get",params={"articles":";".join(articles)})
            else:
                r = await client.post("https://oz.wbcon.su/put",json={"article":int(articles[0])})
    except (httpx.HTTPError, ImportError, ValueError) as error:
        raise ParseError("WBCON сейчас недоступен. Попробуйте позже.") from error
    step("WBCON " + market.upper(), f"HTTP {r.status_code}")
    if r.status_code == 429:
        retry = r.headers.get("Retry-After","")
        seconds = max(60,int(retry)) if retry.isascii() and retry.isdigit() and len(retry) <= 6 else 60
        _blocked_until[market] = time.monotonic()+seconds
        raise ParseError(f"WBCON достиг лимита запросов. Повторите через {seconds} с.")
    if r.status_code != 200:
        raise ParseError(f"WBCON не вернул карточку (HTTP {r.status_code}).")
    try:
        return r.json()
    except ValueError as error:
        raise ParseError("WBCON вернул ответ без данных товара.") from error


async def lookup_batch(products):
    if not products or len(products) > 5 or not all(available(p) for p in products):
        raise ParseError("Проверка WBCON требует от одного до пяти подходящих артикулов.")
    market = products[0].marketplace
    if any(p.marketplace != market for p in products) or (market == "ozon" and len(products) != 1):
        raise ParseError("Один запрос поддерживает список WB или одну карточку Ozon.")
    skus = [sku_in_url(httpx.URL(p.url).path) for p in products]
    async with _locks[market]:
        now = time.monotonic()
        found = {}
        missing = {}
        for sku, product in zip(skus, products):
            hit = _cache.get((market, sku))
            if hit is not None and now - hit[0] < _cache_ttl:
                found[sku] = hit[1]
                step("WBCON " + market.upper(), f"артикул {sku} · ответ {int(now-hit[0])} с назад")
            else:
                missing[sku] = product
        if missing:
            try:
                payload = await request_prices(market, list(missing))
            except ParseError as error:
                if not found:
                    raise
                for sku, product in missing.items():
                    found[sku] = Product(market, "", product.url, import_note=str(error))
            else:
                for sku, product in missing.items():
                    result = from_payload(product, sku, payload)
                    found[sku] = result
                    if result.price and (market == "wb" or result.title and result.image_urls):
                        _cache[(market, sku)] = (time.monotonic(), result)
                        _cache.move_to_end((market, sku))
                        if len(_cache) > 256:
                            _cache.popitem(last=False)
        return [replace(found[sku], url=p.url, image_urls=list(found[sku].image_urls))
                for sku, p in zip(skus, products)]


async def lookup(product):
    if not available(product):
        raise ParseError("WBCON нужен прямой артикул. Для WB доступна общая цена без выбранного размера и ПВЗ.")
    return (await lookup_batch([product]))[0]
