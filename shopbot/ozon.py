"""Карточка Ozon: виджеты витрины, SEO-данные и страница товара."""
from __future__ import annotations

import os
import re
from dataclasses import replace

from card import Product
from diagnostics import step
from links import MARKET_DOMAINS, resolve_product_url
from net import impersonation_enabled, make_client
from storefront import as_list, decode, from_jsonld, from_page, from_widget, merged, node_matches, product_nodes, read_source, sku_in_url

# User-Agent не задаётся: его выставляет отпечаток Chrome. Чужой UA с рукопожатием браузера выдаёт бота.
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9", "Referer": "https://www.ozon.ru/"}
DOMAINS = MARKET_DOMAINS["ozon"]
ORIGIN = "https://www.ozon.ru"
API_URL = ORIGIN + "/api/composer-api.bx/page/json/v2"
# Второй адрес спрашивается, только если первый не дал данных.
API_URLS = (API_URL, ORIGIN + "/api/entrypoint-api.bx/page/json/v2")
BLOCK_HINTS = ("HTTP 403", "HTTP 307", "проверки доступа", "перенаправлени")


def from_composer(base, data):
    if not isinstance(data, dict):
        return base
    seo = data.get("seo")
    scripts = as_list(seo.get("script")) if isinstance(seo, dict) else []
    docs = [(s.get("innerHTML") or s.get("content") or s) if isinstance(s, dict) else s for s in scripts]
    nodes = [node for doc in docs for node in product_nodes(doc)]
    sku = sku_in_url(base.url)
    if nodes and all(node_matches(node, sku) is False for node in nodes):
        return base
    result = base
    widgets = data.get("widgetStates") or {}
    for key, raw in widgets.items() if isinstance(widgets, dict) else ():
        widget = decode(raw)
        if not isinstance(widget, dict):
            continue
        name = key.split("-", 1)[0]
        result = merged(result, from_widget(base, name, widget))

    if isinstance(seo, dict):
        structured = from_jsonld(base, docs)
        # SEO title may also be the heading of an error page. Use it only with product evidence.
        title = seo.get("title")
        if not structured.title and isinstance(title, str) and (structured.price or structured.image_urls or result.image_urls or result.price):
            structured = replace(structured, title=title.strip())
        result = merged(result, structured)
    return result


def looks_blocked(notes) -> bool:
    return any(hint in note for note in notes for hint in BLOCK_HINTS)


async def _from_api(client, base: Product, path: str):
    """Виджеты витрины. При блокировке один раз «прогреваем» сессию главной страницей."""
    notes, warmed = [], False
    for endpoint in API_URLS:
        for attempt in range(2):
            api, note = await read_source(client, endpoint, DOMAINS, "Данные Ozon", params={"url": path})
            if api is not None:
                result = from_composer(base, decode(api.text))
                if result.title or result.price or result.image_urls:
                    return result, notes
                note = "Данные Ozon — ответ без полей товара"
                notes.append(note)
                break
            if attempt == 0 and not warmed and looks_blocked([note]) and "HTTP 429" not in note:
                warmed = True
                await read_source(client, ORIGIN + "/", DOMAINS, "Главная Ozon", page=True, retries=0)
                continue
            notes.append(note)
            break
        if any("HTTP 429" in note for note in notes):
            break
    return base, notes


async def fetch_ozon(url: str) -> Product:
    page = None
    notes = []
    async with make_client(headers=HEADERS, timeout=6, proxy=os.getenv("OZON_PROXY") or None, impersonate=True) as client:
        target = await resolve_product_url(client, url, DOMAINS,
                                           lambda u: u.path.startswith("/product/") and bool(sku_in_url(u)), "Ozon")
        path = re.match(r"^/product/[^/]+", target.path).group() + "/"
        base = Product("ozon", "", ORIGIN + path)
        step("Артикул Ozon", sku_in_url(base.url))
        result, notes = await _from_api(client, base, path)
        step("Виджеты Ozon", " · ".join(k for k, v in (("название", result.title), ("цена", result.price), ("галерея", result.image_urls)) if v) or "данных нет")
        # HTML иногда содержит поля, которые витрина не включила в JSON.
        card_only = bool(result.conditional_price and result.price_note == result.conditional_note)
        if not (result.title and result.price and result.image_urls) or card_only:
            if not any("HTTP 429" in note for note in notes):
                page, note = await read_source(client, base.url, DOMAINS, "Страница Ozon", page=True)
                if note:
                    notes.append(note)
            if page is not None:
                result = merged(result, from_page(base, page))
    if not (result.title and result.price and result.image_urls):
        reason = "Ozon сообщает, что товар отсутствует." if result.price_unavailable else (
            ". ".join(dict.fromkeys(notes)) or "Ozon вернул карточку с неполными данными."
        )
        if not result.price_unavailable and looks_blocked(notes) and not impersonation_enabled():
            reason += ". Ozon проверяет отпечаток браузера: установите curl_cffi (pip install curl_cffi) и перезапустите бота"
        result = replace(result, import_note=reason)
    return result
