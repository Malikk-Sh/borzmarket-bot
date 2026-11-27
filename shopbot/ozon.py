"""Карточка Ozon: публичная страница товара — JSON-LD, Open Graph и виджеты витрины в HTML.

v10 сначала открывает саму страницу товара (то же, что открывает браузер или превью
Telegram): в ней есть schema.org Product с ценой и виджет webPrice с условием оплаты.
Внутренний JSON-API витрины больше не спрашивается: при блокировке он отвечал теми же
403, а каждый лишний запрос повышал оценку антибота. Короткая ссылка раскрывается до
артикула, а адрес перенаправления даёт черновое название, даже если страница закрыта.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import replace

import httpx

from card import PROVISIONAL_TITLE, ParseError, Product
from diagnostics import Timer, event, step
from links import MARKET_DOMAINS, LinkError, allowed_url, resolve_product_url
from net import impersonation_enabled, make_client
from slug import slug_of, title_from_slug
import state
from storefront import as_list, decode, from_jsonld, from_page, from_widget, merged, node_matches, product_nodes, read_source, sku_in_url

# User-Agent задаёт отпечаток Chrome (или net.BROWSER_UA без него).
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}
DOMAINS = MARKET_DOMAINS["ozon"]
ORIGIN = "https://www.ozon.ru"
BLOCK_HINTS = ("HTTP 403", "HTTP 307", "проверки доступа", "перенаправлени")
PAGE_PAUSE = 90  # после блокировки остальные товары подборки не трогают Ozon полторы минуты
SHORT_HOSTS = ("ozon.ru", "www.ozon.ru")


def from_composer(base, data):
    """Разбор JSON витрины (тот же набор виджетов, что и в HTML). Нужен для сохранённых ответов."""
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
        title = seo.get("title")
        if not structured.title and isinstance(title, str) and (structured.price or structured.image_urls or result.image_urls or result.price):
            structured = replace(structured, title=title.strip())
        result = merged(result, structured)
    return result


def looks_blocked(notes) -> bool:
    return any(hint in note for note in notes for hint in BLOCK_HINTS)


def outcome_of(note: str) -> tuple[str, int | None]:
    code = re.search(r"HTTP (\d{3})", note)
    http = int(code.group(1)) if code else None
    if http == 429:
        return "limit", http
    if "проверки доступа" in note or http in (403, 401, 498, 307):
        return "blocked", http
    if "тайм-аут" in note:
        return "timeout", http
    return "error", http


async def resolve(client, url: str) -> httpx.URL:
    """Адрес карточки из прямой или короткой ссылки. Короткую пробуем на двух хостах Ozon.

    27.11.2026 из облака ozon.ru/t/377XHKT дважды ответил 403, а www.ozon.ru/t/377XHKT сразу
    дал 301 на товар. Второй хост стоит одного запроса.
    """
    current = allowed_url(url, DOMAINS)

    def is_product(u):
        return u.path.startswith("/product/") and bool(sku_in_url(u))
    if is_product(current):
        return current
    hosts = [current.host]
    if current.path.startswith("/t/") and current.host in SHORT_HOSTS:
        hosts += [host for host in SHORT_HOSTS if host != current.host]
    error: ParseError | None = None
    for index, host in enumerate(hosts):
        if index:
            await asyncio.sleep(0.3)
        target = None
        with Timer() as timer:
            try:
                target = await resolve_product_url(client, current.copy_with(host=host), DOMAINS, is_product, "Ozon")
            except LinkError:
                raise  # чужой домен в перенаправлении: второй хост не поможет
            except ParseError as exc:
                error = exc
        if target is not None:
            event("ozon.short", "ok", ms=timer.ms, detail=host)
            return target
        outcome, http = outcome_of(str(error))
        event("ozon.short", outcome, http=http, ms=timer.ms, detail=host)
    raise error or ParseError("Ozon не раскрыл короткую ссылку. Пришлите /ozon и артикул из приложения.")


def _fields(product) -> tuple[str, ...]:
    return tuple(name for name, value in (("название", product.title), ("цена", product.price),
                                           ("фото", product.image_urls)) if value)


async def fetch_ozon(url: str, *, fresh: bool = False) -> Product:
    notes: list[str] = []
    started = time.time()
    async with make_client(headers=HEADERS, timeout=8, proxy=os.getenv("OZON_PROXY") or None, impersonate=True) as client:
        target = await resolve(client, url)
        path = re.match(r"^/product/[^/]+", target.path).group() + "/"
        base = Product("ozon", "", ORIGIN + path)
        sku = sku_in_url(base.url)
        step("Артикул Ozon", sku)
        hint = title_from_slug(slug_of(path))
        result = base
        pause = state.STATE.blocked("ozon.page")
        if pause and not fresh:
            note = f"Страница Ozon — пауза после блокировки, {state.wait_text(pause[0])}"
            notes.append(note)
            step("Страница Ozon", "пауза после блокировки")
            event("ozon.page", "skipped", retry_at=pause[0], detail=pause[1])
        else:
            info: dict = {}
            page, note = await read_source(client, base.url, DOMAINS, "Страница Ozon", page=True, timeout=10, info=info)
            if page is not None:
                result = from_page(base, page)
                got = _fields(result)
                event("ozon.page", "ok" if got else "empty", http=page.status_code, ms=info.get("ms"),
                      fields=got, attempts=info.get("attempts", 1))
                state.STATE.clear("ozon.page")
                if not got:
                    notes.append("Страница Ozon — в ответе нет данных этого товара")
            else:
                notes.append(note)
                outcome, http = outcome_of(note)
                retry_at = None
                if outcome in ("blocked", "limit"):
                    retry_at = time.time() + max(PAGE_PAUSE, info.get("retry_after") or 0)
                    state.STATE.block("ozon.page", retry_at, note)
                event("ozon.page", outcome, http=http, ms=info.get("ms"), retry_at=retry_at,
                      attempts=info.get("attempts", 1))
    if result.title and not result.title_source:
        result = replace(result, title_source="ozon.page")
    if result.price is not None and result.price_at is None:
        result = replace(result, price_at=int(started))
    if not result.title and hint:
        # Ozon сам назвал товар в адресе перенаправления. Это черновик: его подтвердит человек.
        result = replace(result, title=hint, title_source=PROVISIONAL_TITLE)
        step("Название из адреса", "черновое, нужна проверка")
    if not (result.title and result.price and result.image_urls) or result.provisional_title:
        reason = "Ozon сообщает, что товар отсутствует." if result.price_unavailable else (
            ". ".join(dict.fromkeys(notes)) or "Ozon вернул карточку с неполными данными."
        )
        if not result.price_unavailable and looks_blocked(notes) and not impersonation_enabled():
            reason += ". Ozon проверяет отпечаток браузера: установите curl_cffi (pip install curl_cffi) и перезапустите бота"
        result = replace(result, import_note=reason)
    return result
