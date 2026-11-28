"""Резервный источник WB и ручная проверка Ozon через публичные методы WBCON.

Лимиты из описания OpenAPI (https://prices.wbcon.su/openapi.json, https://oz.wbcon.su/openapi.json,
прочитано 27.11.2026): демо — 2 запроса в минуту на метод, до 5 артикулов WB в запросе через «;».
WB: salePriceU — «цена после скидки и СПП» в копейках, без WB Кошелька, без региона и размера.
Ozon (сервис «Ozon-калькулятор»): name, imageUrl, price по артикулу.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from collections import OrderedDict, deque
from dataclasses import replace
from decimal import Decimal

import httpx

from diagnostics import event, step
from card import ParseError, Product, parse_price
import state
from storefront import images, retry_after, sku_in_url

DEMO_PER_MINUTE = 2
DEMO_BATCH = 5
# У каждого демо-метода свой лимит. Проверка и резервирование выполняются до первого await.
_recent = {"wb": deque(), "ozon": deque()}
_blocked_until = {"wb": 0.0, "ozon": 0.0}  # совместимость: зеркало паузы из state

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


def paused(market: str) -> tuple[float, str] | None:
    """Пауза после 429 (переживает перезапуск, если подключена база)."""
    value = state.STATE.blocked("wbcon." + market)
    if value is None and _blocked_until[market] > time.time():
        value = (_blocked_until[market], "лимит запросов")
    return value


def reserve(market, *, background: bool = False):
    now = time.monotonic()
    window = _recent[market]
    while window and window[0] <= now - 60:
        window.popleft()
    pause = paused(market)
    if pause:
        raise ParseError(f"WBCON на паузе после лимита. Повторите {state.wait_text(pause[0])}.")
    if background and window:
        # Фоновые проверки не отнимают квоту у администратора: только при пустом окне.
        raise ParseError("WBCON занят интерактивными запросами, фоновая проверка отложена.")
    deadline = window[0] + 60 if len(window) >= DEMO_PER_MINUTE else 0
    if deadline > now:
        raise ParseError(f"Лимит WBCON. Попробуйте через {math.ceil(deadline - now)} с.")
    window.append(now)


def from_payload(product, sku, payload):
    result = Product(product.marketplace, "", product.url)
    if product.marketplace == "wb":
        rows = payload if isinstance(payload, list) else []
        rows = [r for r in rows if isinstance(r, dict) and str(r.get("id")) == sku]
        if len(rows) == 1:
            value = str(rows[0].get("salePriceU", ""))
            if value.isascii() and value.isdigit() and len(value) <= 16 and int(value) > 0:
                result.price = Decimal(value).scaleb(-2)
            supplier, supplier_id = rows[0].get("supplier"), rows[0].get("supplierId")
            if isinstance(supplier, str) and supplier.strip():
                result.seller = f"{supplier.strip()[:60]} ({supplier_id})" if supplier_id else supplier.strip()[:60]
            result.region = "не указан сервисом"
    elif isinstance(payload, dict) and str(payload.get("article")) == sku:
        title = payload.get("name")
        result.title = title.strip() if isinstance(title, str) else ""
        result.image_urls = images(product, [payload.get("imageUrl")])
        result.price = parse_price(payload.get("price"))
        if result.title:
            result.title_source = "wbcon"
    if result.price:
        result.price_note = "по данным WBCON"
        result.price_source = "wbcon"
        # Сервис не сообщает, когда он видел эту цену: известен только момент нашего запроса.
        result.price_at = None
        result.price_fetched_at = int(time.time())
    return result


async def request_prices(market, articles, *, background: bool = False):
    reserve(market, background=background)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            if market == "wb":
                r = await client.get("https://prices.wbcon.su/get", params={"articles": ";".join(articles)})
            else:
                r = await client.post("https://oz.wbcon.su/put", json={"article": int(articles[0])})
    except (httpx.HTTPError, ImportError, ValueError) as error:
        outcome = "timeout" if isinstance(error, httpx.TimeoutException) else "error"
        event("wbcon." + market, outcome, ms=int((time.perf_counter() - started) * 1000),
              detail=type(error).__name__)
        raise ParseError("WBCON сейчас недоступен. Попробуйте позже.") from error
    ms = int((time.perf_counter() - started) * 1000)
    if r.status_code == 429:
        wait = retry_after(r.headers.get("Retry-After"))
        seconds = max(60, math.ceil(wait)) if wait is not None and wait < 86400 else 60
        until = time.time() + seconds
        step("WBCON " + market.upper(), f"HTTP 429 · повтор {state.wait_text(until)}")
        _blocked_until[market] = until
        state.STATE.block("wbcon." + market, until, "HTTP 429")
        event("wbcon." + market, "limit", http=429, ms=ms, retry_at=until)
        raise ParseError(f"WBCON достиг лимита запросов. Повторите {state.wait_text(until)}.")
    if r.status_code != 429:
        step("WBCON " + market.upper(), f"HTTP {r.status_code} · {ms / 1000:.1f} с".replace(".", ","))
    if r.status_code != 200:
        event("wbcon." + market, "error", http=r.status_code, ms=ms)
        raise ParseError(f"WBCON не вернул карточку (HTTP {r.status_code}).")
    try:
        payload = r.json()
    except ValueError as error:
        event("wbcon." + market, "error", http=200, ms=ms, detail="ответ без JSON")
        raise ParseError("WBCON вернул ответ без данных товара.") from error
    event("wbcon." + market, "ok", http=200, ms=ms, detail=f"артикулов {len(articles)}")
    return payload


async def lookup_batch(products, *, background: bool = False):
    if not products or len(products) > DEMO_BATCH or not all(available(p) for p in products):
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
                event("wbcon." + market, "cached", detail=f"ответ {int(now - hit[0])} с назад")
            else:
                missing[sku] = product
        if missing:
            try:
                payload = await request_prices(market, list(missing), **({"background": True} if background else {}))
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


async def lookup(product, *, background: bool = False):
    if not available(product):
        raise ParseError("WBCON нужен прямой артикул. Для WB доступна общая цена без выбранного размера и ПВЗ.")
    return (await lookup_batch([product], background=background))[0]
