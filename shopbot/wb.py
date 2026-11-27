"""Wildberries: данные карточки через публичные эндпоинты витрины (без авторизации).

Эндпоинты неофициальные, WB периодически их меняет. Если что-то сломалось,
править нужно только этот файл: DETAIL_ENDPOINTS и разбор цен в _parse_prices.
"""
from __future__ import annotations

import asyncio
import os
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone, timedelta
import re
from store_prices import kopecks, wb_quote

import httpx

from diagnostics import step
from card import ParseError, Product, fmt_price
from net import TTLCache, make_client
from storefront import decode, from_page, read_source
from links import IMAGE_DOMAINS, MARKET_DOMAINS, LinkError, get_allowed, resolve_product_url

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Живая версия сейчас одна: v4. Старые v1 и v2 у WB отвечают 404, поэтому их в списке нет.
# Второй адрес — тот же v4 через сайт. Первым спрашивается тот, что отвечал в прошлый раз.
DETAIL_ENDPOINTS = (
    "https://card.wb.ru/cards/v4/detail",
    "https://www.wildberries.ru/__internal/u-card/cards/v4/detail",
)
DETAIL_PARAMS = {"appType": "1", "curr": "rub", "dest": "-1257786", "spp": "30", "locale": "ru"}  # Москва
DOMAINS = MARKET_DOMAINS["wb"]

MEDIA_RANGES = json.loads(Path(__file__).with_name("wb_media_ranges.json").read_text(encoding="utf-8"))
MAX_BASKET = 90  # WB добавляет basket-серверы; таблица выше уже доходит до 65
BASKET_CONCURRENCY = 5  # ограничиваем параллелизм, чтобы не ловить лимиты
MAX_PROBE_FAILURES = 6  # столько сетевых сбоев подряд прекращают поиск
_basket_cache: dict[int, int] = {}  # vol -> номер basket
_DETAIL_CACHE = TTLCache(90, 128)  # (артикул, регион) -> ответ витрины; заполняется пакетным запросом
_endpoint_order: list[str] = list(DETAIL_ENDPOINTS)


def extract_nm(url: httpx.URL) -> int | None:
    m = re.search(r"/catalog/([0-9]{1,16})(?:/|$)", url.path)
    return int(m.group(1)) if m else None


async def _resolve_product(client: httpx.AsyncClient, url: str) -> tuple[int, int | None]:
    """Достаёт артикул и выбранный размер из прямой или короткой ссылки."""
    target = await resolve_product_url(client, url, DOMAINS, lambda u: extract_nm(u) is not None, "WB")
    nm = extract_nm(target)
    sizes = target.params.get_list("size")
    if not sizes:
        return nm, None
    if len(set(sizes)) != 1 or not re.fullmatch(r"[1-9][0-9]{0,15}", sizes[0]):
        raise ParseError("В ссылке WB указан некорректный размер. Пришлите ссылку из карточки заново.")
    return nm, int(sizes[0])


def _dest() -> str:
    return os.getenv("WB_DEST") or DETAIL_PARAMS["dest"]


def _products(response, wanted: set[int]) -> dict[int, dict]:
    """Товары из ответа витрины. Чужие и подставные записи отбрасываются по артикулу."""
    payload = decode(response.text)
    data = (payload.get("data") or payload) if isinstance(payload, dict) else None
    rows = data.get("products") if isinstance(data, dict) else None
    found = {}
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and str(row.get("id")).isdigit() and int(row["id"]) in wanted:
            found[int(row["id"])] = row
    return found


def _remember_endpoint(endpoint: str) -> None:
    if endpoint in _endpoint_order:
        _endpoint_order.remove(endpoint)
    _endpoint_order.insert(0, endpoint)


async def prefetch_details(client, nms) -> None:
    """Один запрос на всю подборку. WB принимает несколько артикулов через «;».

    Ответы кладутся в короткий кэш, и _detail берёт их оттуда без новых запросов.
    Сбой не страшен: каждый товар тогда запрашивается по отдельности.
    """
    nms = list(dict.fromkeys(int(nm) for nm in nms))
    if len(nms) < 2:
        return
    dest = _dest()
    params = {**DETAIL_PARAMS, "dest": dest, "nm": ";".join(map(str, nms))}
    for endpoint in list(_endpoint_order):
        response, note = await read_source(client, endpoint, DOMAINS, "Цены WB, пакет", params=params)
        if note:
            if any(code in note for code in ("HTTP 403", "HTTP 429", "HTTP 498")):
                return
            continue
        found = _products(response, set(nms))
        for nm, row in found.items():
            _DETAIL_CACHE.set((nm, dest), row)
        if found:
            _remember_endpoint(endpoint)
            return


async def _detail(client, nm, size_id, url):
    """Keep identifying fields while looking for a response with a usable price."""
    title = ""
    notes = []
    dest = _dest()
    params = {**DETAIL_PARAMS, "dest": dest, "nm": nm}

    def parse(row):
        nonlocal title
        if isinstance(row.get("name"), str):
            title = title or row["name"].strip()
        parsed = wb_quote(Product("wb", title, url), row, size_id)
        if parsed.price is not None or parsed.price_unavailable:
            if parsed.price_unavailable:
                parsed.import_note = "WB сообщает, что товар или выбранный размер отсутствует."
            return parsed
        return None

    cached = _DETAIL_CACHE.get((nm, dest))
    if cached is not None and (result := parse(cached)) is not None:
        step("Цены WB", "из пакетного запроса")
        return result

    for endpoint in list(_endpoint_order):
        response, note = await read_source(client, endpoint, DOMAINS, "Цены WB " + urlsplit(endpoint).netloc, params=params)
        if note:
            notes.append(note)
            if any(code in note for code in ("HTTP 403", "HTTP 429", "HTTP 498")):
                break
            continue
        row = _products(response, {nm}).get(nm)
        if row is None:
            continue
        if (result := parse(row)) is not None:
            _remember_endpoint(endpoint)
            return result

    base = Product("wb", "", url)
    markup = base
    if not any("HTTP 429" in note for note in notes):
        response, note = await read_source(client, url, DOMAINS, "Страница WB", page=True)
        if response is not None:
            markup = from_page(base, response)
        if note:
            notes.append(note)
    markup.title = title or markup.title
    if size_id is not None or dest != DETAIL_PARAMS["dest"]:
        # Public metadata generally describes the whole product, not the chosen size.
        markup.price = markup.old_price = None
        markup.price_unavailable = False
        markup.price_source = ""
    if markup.price is None:
        notes = list(dict.fromkeys(notes))
        markup.import_note = ". ".join(notes) or "WB не указал цену для выбранного размера или региона."
    return markup


def predicted_basket(vol: int) -> int | None:
    for first, last, host in MEDIA_RANGES:
        if first <= vol <= last:
            match = re.fullmatch(r"basket-(\d+)\.wbbasket\.ru", host)
            return int(match.group(1)) if match else None
    if MEDIA_RANGES and vol > MEDIA_RANGES[-1][1]:
        match = re.fullmatch(r"basket-(\d+)\.wbbasket\.ru", MEDIA_RANGES[-1][2])
        return int(match.group(1)) if match else None
    return None


def basket_order(vol: int, cached: int | None = None) -> list[int]:
    """Порядок опроса: сначала предполагаемый сервер, затем ближайшие к нему.

    Таблица диапазонов у WB дрейфует, ошибка почти всегда в один-два номера.
    """
    base = cached if cached is not None else predicted_basket(vol)
    if base is None:
        return list(range(MAX_BASKET, 0, -1))  # новые артикулы лежат на новых серверах
    order = [base]
    for distance in range(1, MAX_BASKET):
        for n in (base + distance, base - distance):
            if 1 <= n <= MAX_BASKET:
                order.append(n)
    return order


async def _find_basket(client: httpx.AsyncClient, nm: int) -> tuple[int, dict] | None:
    """Ищет basket-сервер с фото и данными карточки."""
    vol, part = nm // 100000, nm // 1000
    sem = asyncio.Semaphore(BASKET_CONCURRENCY)
    failures = 0

    async def probe(n: int) -> tuple[int, dict] | None:
        nonlocal failures
        url = f"https://basket-{n:02d}.wbbasket.ru/vol{vol}/part{part}/{nm}/info/ru/card.json"
        async with sem:
            if failures >= MAX_PROBE_FAILURES:
                return None
            try:
                r = await get_allowed(client, url, IMAGE_DOMAINS["wb"])
            except (LinkError, httpx.HTTPError):
                failures += 1  # один тайм-аут не отменяет поиск, а подряд шесть отменяют
                return None
        if r.status_code == 200:
            try:
                card = r.json()
            except ValueError:
                return None
            if isinstance(card, dict) and (card.get("imt_name") or card.get("media")) and str(card.get("nm_id", nm)) == str(nm):
                return n, card
        elif r.status_code != 404:
            failures += 1
        return None

    order = basket_order(vol, _basket_cache.get(vol))
    first = await probe(order[0])
    if first:
        _basket_cache[vol] = first[0]
        return first

    tasks = [asyncio.create_task(probe(n)) for n in order[1:]]
    try:
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res:
                _basket_cache[vol] = res[0]
                return res
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None


def _parse_prices(prod, size_id=None):
    parsed = wb_quote(Product("wb", "", ""), prod, size_id)
    return parsed.price, parsed.old_price


async def _basket_with_deadline(client, nm):
    try:
        return await asyncio.wait_for(_find_basket(client, nm), 15)
    except asyncio.TimeoutError:
        return None


async def _history_hint(client, nm, basket_n):
    """A dated reference for the editor; never enters the price or the monitor."""
    url = f"https://basket-{basket_n:02d}.wbbasket.ru/vol{nm//100000}/part{nm//1000}/{nm}/info/price-history.json"
    response, _ = await read_source(client, url, IMAGE_DOMAINS["wb"], "История цен WB")
    rows = decode(response.text) if response is not None else None
    if not isinstance(rows, list):
        return ""
    now = int(time.time())
    best_time, best_price = 0, None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("price"), dict):
            continue
        stamp = row.get("dt")
        amount = kopecks(row["price"].get("RUB"))
        if type(stamp) is int and 0 < stamp <= now and stamp > best_time and amount:
            best_time, best_price = stamp, amount
    if not best_price:
        return ""
    day = datetime.fromtimestamp(best_time, tz=timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
    return f"В истории WB за {day} — {fmt_price(best_price)}. Проверьте сегодняшнюю цену в приложении."


async def fetch_wb(url: str) -> Product:
    async with make_client(headers=HEADERS, timeout=5, proxy=os.getenv("WB_PROXY") or None, impersonate=True) as client:
        nm, size_id = await _resolve_product(client, url)
        product_url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
        if size_id is not None:
            product_url += f"?size={size_id}"
        result, basket = await asyncio.gather(
            _detail(client, nm, size_id, product_url), _basket_with_deadline(client, nm)
        )
        if basket and not result.price_unavailable and result.price is None and size_id is None and "HTTP 429" not in result.import_note:
            hint = await _history_hint(client, nm, basket[0])
            if hint:
                result.import_note = result.import_note.rstrip(".") + ". " + hint
    basket_n, card = basket if basket else (None, {})
    step("Каталог фото WB", f"basket-{basket_n:02d}" if basket_n else "карточка не получена")
    title = card.get("imt_name")
    if not result.title and isinstance(title, str):
        result.title = title.strip()

    images: list[str] = []
    if basket_n is not None:
        vol, part = nm // 100000, nm // 1000
        media = card.get("media")
        count = media.get("photo_count", 1) if isinstance(media, dict) else 1
        if not isinstance(count, int):
            count = 1
        images = [
            f"https://basket-{basket_n:02d}.wbbasket.ru/vol{vol}/part{part}/{nm}/images/big/{i}.webp"
            for i in range(1, min(count, 3) + 1)
        ]

    result.image_urls = list(dict.fromkeys(images + result.image_urls))[:3]
    if result.price is not None:
        result.wb_dest = _dest()
    if not (result.title or result.price or result.image_urls):
        result.import_note = result.import_note or "WB не вернул данные товара."
    return result


async def prefetch_many(urls) -> None:
    """Подборка из нескольких товаров WB: цены всех артикулов за один запрос."""
    nms = []
    for value in urls:
        try:
            nm = extract_nm(httpx.URL(str(value)))
        except (ValueError, httpx.InvalidURL):
            continue
        if nm:
            nms.append(nm)
    if len(set(nms)) < 2:
        return
    try:
        async with make_client(headers=HEADERS, timeout=5, proxy=os.getenv("WB_PROXY") or None, impersonate=True) as client:
            await asyncio.wait_for(prefetch_details(client, nms), 12)
    except (httpx.HTTPError, LinkError, ParseError, asyncio.TimeoutError):
        pass
