"""Парсер цен BorzMarket. Работает отдельно от Telegram.

Порядок источников (v10):
  1. Площадка: Ozon — публичная страница товара, WB — витрина цен и каталог фото.
  2. Память бота: название и ссылки на фото из прошлых импортов; цена — только свежая (до 10 мин),
     иначе справка с датой.
  3. Резерв: WBCON (демо, 2 запроса в минуту), BHAPI (ключ владельца).
Каждое поле хранит источник, а цена — время, когда её показал источник.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from card import ParseError, Product, fmt_price
from diagnostics import capture, capture_events, event, step
from history import product_key
from links import normalize_references, product_link
from ozon import fetch_ozon
import report
import state
from storefront import PageMarkup, from_page, merged, node_matches, product_nodes, sku_in_url
from wb import fetch_wb
import bhapi
import net
import wbcon


def market_url(value):
    market, url = product_link(normalize_references(value.strip()))
    return market, str(url)


# Полные ответы живут три минуты: повторная ссылка, добавление в подборку и «Дальше» не трогают площадку.
# Неполные не кэшируются никогда, чтобы «Загрузить недостающее» всегда делало настоящий запрос.
_RESULT_CACHE = net.TTLCache(180, 64)


def _complete(product) -> bool:
    return bool(product.title and product.price and product.image_urls and not product.price_unavailable
                and not product.provisional_title)


ENGINES = ("combo", "own", "legacy")
AGGREGATOR_TTL = 600  # ответ WBCON/BHAPI переиспользуется 10 минут, в том числе после перезапуска
MANUAL_SOURCES = ("manual", "message", "screenshot", "saved_html")


def engine(market: str) -> str:
    """Режим источника цен для площадки.

    combo  — собственный путь (площадка), затем память и резерв с явной пометкой (по умолчанию);
    own    — только площадка и память бота, без WBCON и BHAPI;
    legacy — порядок v10.0: площадка → память → WBCON → BHAPI без журнала резерва.
    Переменные: PRICE_ENGINE для обеих площадок, OZON_PRICE_ENGINE и WB_PRICE_ENGINE отдельно.
    """
    value = (os.getenv(f"{market.upper()}_PRICE_ENGINE") or os.getenv("PRICE_ENGINE") or "combo").strip().lower()
    return value if value in ENGINES else "combo"


def observation_rows(product) -> list[dict]:
    """Строки журнала: обычная цена и каждая условная отдельно, с контекстом предложения."""
    base = dict(market=product.marketplace, sku=sku_in_url(product.url), variant=product.variant,
                seller=product.seller, region=product.region or product.wb_dest, source=product.price_source,
                observed_at=product.price_at)
    if product.price_unavailable:
        return [dict(base, payment="regular", amount=None)]
    if product.price is None or not product.price_source:
        return []
    rows = []
    conditional_only = product.conditional_price is not None and product.conditional_price == product.price \
        and bool(product.price_note)
    rows.append(dict(base, payment=product.price_note if conditional_only else "regular", amount=str(product.price)))
    if product.conditional_price is not None and not conditional_only:
        rows.append(dict(base, payment=product.conditional_note or "условная", amount=str(product.conditional_price)))
    return rows


def observe(product) -> None:
    """В журнал попадают только ответы источников: не память, не ручной ввод."""
    if product.price_origin == "memory" or product.price_source in MANUAL_SOURCES:
        return
    stamp = product.price_fetched_at or time.time()
    for row in observation_rows(product):
        state.STATE.observe(memory_key(product), row, now=stamp)


def cached_aggregator(product, source: str):
    """Свежий ответ агрегатора из журнала (combo): экономит квоту WBCON между перезапусками."""
    row = state.STATE.last_observation(memory_key(product), source)
    if not row or not row.get("amount") or time.time() - row["last_seen"] > AGGREGATOR_TTL:
        return None
    return row


def memory_key(product) -> str:
    key = product_key(product, variant=True)
    if product.marketplace == "wb":
        key += "@" + (os.getenv("WB_DEST") or "-1257786")
    return key


def remember(product) -> None:
    """В память попадают только поля от источников, без ручного ввода и черновых названий."""
    if product.price_source in ("manual", "message", "screenshot") or product.title_source in ("manual", "message"):
        return
    observe(product)
    if product.price_origin == "memory":
        return  # восстановленное из памяти не перезаписывает память
    keep = product
    if product.provisional_title:
        keep = replace(product, title="", title_source="")
    if keep.title or keep.price is not None or keep.image_urls:
        state.STATE.remember(memory_key(product), keep)


def recall(product) -> Product:
    """Дополняет пустые поля из памяти. Старая цена становится справкой с возрастом."""
    saved = state.STATE.recall(memory_key(product))
    if not saved:
        return product
    fetched_at, data = saved
    age = time.time() - fetched_at
    used = []
    if (not product.title or product.provisional_title) and data.get("title") and age <= state.STABLE_FRESH:
        product = replace(product, title=data["title"], title_source="memory")
        used.append("название")
    if not product.image_urls and data.get("image_urls") and age <= state.STABLE_FRESH:
        product = replace(product, image_urls=list(data["image_urls"])[:3])
        used.append("ссылки на фото")
    price = data.get("price")
    if product.price is None and not product.price_unavailable and price is not None:
        observed = data.get("price_at") or fetched_at
        price_age = time.time() - observed
        label = report.PRICE_SOURCES.get(data.get("price_source") or "", data.get("price_source") or "источник")
        if price_age <= state.PRICE_FRESH:
            product = replace(product, price=Decimal(price),
                              old_price=Decimal(data["old_price"]) if data.get("old_price") else None,
                              price_note=data.get("price_note") or "",
                              conditional_price=Decimal(data["conditional_price"]) if data.get("conditional_price") else None,
                              conditional_note=data.get("conditional_note") or "",
                              price_source=data.get("price_source") or "memory",
                              price_at=data.get("price_at"), price_fetched_at=int(fetched_at),
                              price_origin="memory", seller=data.get("seller") or "",
                              variant=data.get("variant") or "", wb_dest=data.get("wb_dest") or "")
            used.append(f"цена ({state.age_text(price_age)})")
        else:
            when = datetime.fromtimestamp(observed, report.MOSCOW).strftime("%d.%m %H:%M")
            note = (f"Последняя известная цена {fmt_price(Decimal(price))} ({label}, {when} МСК, "
                    f"{state.age_text(price_age)}). Для поста нужна сегодняшняя цена.")
            product = replace(product, reference_note=product.reference_note or note)
    if used:
        step("Память бота", ", ".join(used))
        event("memory", "cached", detail=", ".join(used) + f" · {state.age_text(age)}")
    return product


async def fetch_product(value, *, direct_only=False, allow_paid=True, fresh=False, background=False):
    market, url = market_url(value)
    key = (market, url)
    result = None if fresh else _RESULT_CACHE.get(key)
    if result is not None:
        result = copy.deepcopy(result)
        step("Кэш", "ответ площадки до трёх минут назад")
        event("memory", "cached", detail="полный ответ площадки до 3 мин назад")
    else:
        def load():
            async def run():
                with capture_events() as events:
                    product = await (fetch_ozon(url, fresh=fresh) if market == "ozon" else fetch_wb(url, fresh=fresh))
                product.trace = list(events)
                return product
            return run()
        result = await load() if fresh else await net.once(key, load)
        result = copy.copy(result)
        result.trace = list(result.trace)
        remember(result)
        if _complete(result):
            _RESULT_CACHE.set(key, copy.deepcopy(result))
    with capture_events() as recalled:
        result = recall(result)
    result.trace = list(result.trace) + recalled
    if direct_only:
        return finish(result)
    return (await supplement_products([result], allow_paid=allow_paid, background=background))[0]


def finish(product, has_photo: bool | None = None) -> Product:
    """Записывает причины пропусков по полям. has_photo=None — оцениваем по ссылкам."""
    photo = bool(product.image_urls) if has_photo is None else has_photo
    product.gaps = report.field_gaps(product, photo, product.trace)
    return product


async def supplement_wb(products, *, background=False):
    """Объединяет недостающие цены WB из одной подборки в запросы по пять артикулов."""
    output = list(products)
    mode = engine("wb")
    if os.getenv("WB_PRICE_FALLBACK", "1") != "1" or mode == "own" or (background and mode == "combo"):
        return output
    pending = [i for i, p in enumerate(output) if isinstance(p, Product) and p.marketplace == "wb"
               and p.price is None and wbcon.available(p)]
    if mode == "combo":
        for i in list(pending):
            row = cached_aggregator(output[i], "wbcon")
            if row is None:
                continue
            age = time.time() - row["last_seen"]
            with capture_events() as events:
                event("wbcon.wb", "cached", detail=f"ответ WBCON {state.age_text(age)}")
            output[i] = replace(output[i], price=Decimal(row["amount"]), price_note="по данным WBCON",
                                price_source="wbcon", used_fallback=True, price_at=None,
                                price_fetched_at=int(row["last_seen"]), price_origin="aggregator",
                                seller=row.get("seller") or output[i].seller, region=row.get("region") or "",
                                variant="без выбора размера", trace=list(output[i].trace) + events,
                                import_note="Цена из WBCON (сохранённый ответ). Сверьте её в приложении перед публикацией.")
            pending.remove(i)
    for start in range(0, len(pending), wbcon.DEMO_BATCH):
        indexes = pending[start:start + wbcon.DEMO_BATCH]
        with capture_events() as events:
            try:
                quotes = await wbcon.lookup_batch([output[i] for i in indexes], background=background)
            except ParseError as error:
                quotes = None
                failure = str(error)
        for i in indexes:
            output[i].trace = list(output[i].trace) + events
        if quotes is None:
            for i in indexes:
                output[i] = replace(output[i], import_note=(output[i].import_note + " " + failure).strip())
            continue
        for i, quote in zip(indexes, quotes):
            p = output[i]
            if quote.price is None:
                reason = quote.import_note or "WBCON не указал положительную цену."
                output[i] = replace(p, import_note=(p.import_note + " " + reason).strip())
            else:
                output[i] = replace(p, price=quote.price, price_note=quote.price_note, price_source="wbcon",
                                    used_fallback=True, price_at=None,
                                    price_fetched_at=quote.price_fetched_at or int(time.time()),
                                    seller=quote.seller or p.seller, region=quote.region,
                                    variant="без выбора размера", price_origin="aggregator",
                                    import_note="Цена из WBCON. Сверьте её в приложении перед публикацией.")
                remember(output[i])
    return output


async def supplement_products(products, *, allow_paid=True, background=False):
    """WB объединяется в пакет. Для Ozon демо принимает по одному артикулу."""
    output = await supplement_wb(products, background=background)
    for index, product in enumerate(output):
        if not isinstance(product, Product):
            continue
        if product.price_unavailable or not sku_in_url(product.url):
            output[index] = finish(product)
            continue
        providers = []
        mode = engine(product.marketplace)
        if mode == "own":
            output[index] = finish(product)
            continue
        if product.marketplace == "ozon" and os.getenv("OZON_PRICE_FALLBACK", "1") == "1" \
                and not (background and mode == "combo"):
            if mode == "combo" and product.price is None and (row := cached_aggregator(product, "wbcon")):
                with capture_events() as events:
                    event("wbcon.ozon", "cached", detail=f"ответ WBCON {state.age_text(time.time() - row['last_seen'])}")
                product = replace(product, price=Decimal(row["amount"]), price_note="по данным WBCON",
                                  price_source="wbcon", used_fallback=True, price_at=None,
                                  price_fetched_at=int(row["last_seen"]), price_origin="aggregator",
                                  trace=list(product.trace) + events)
            providers.append(("WBCON", lambda p: wbcon.lookup(p, background=background)))
        if allow_paid and not background and bhapi.token(product.marketplace):
            providers.append(("BHAPI", bhapi.lookup))
        for label, lookup in providers:
            if product.title and product.price and product.image_urls and not product.provisional_title:
                break
            with capture_events() as events:
                try:
                    extra = await lookup(product)
                    failure = None
                except ParseError as error:
                    extra, failure = None, error
            product.trace = list(product.trace) + events
            if failure is not None:
                step(label, str(failure))
                product = replace(product, import_note=(product.import_note + " " + str(failure)).strip())
                continue
            fields = [name for name, value in (("название", extra.title), ("цена", extra.price),
                                                ("галерея", extra.image_urls)) if value]
            step(label, ", ".join(fields) or "поля не получены")
            note = product.import_note
            previous = product
            if previous.provisional_title and extra.title:
                product = replace(product, title="", title_source="")
            product = merged(product, extra)
            if previous.title and product.title == previous.title:
                product.title_source = previous.title_source
            elif product.title and product.title == extra.title:
                product.title_source = extra.title_source or label.lower()
            product.trace = previous.trace
            product.reference_note = previous.reference_note
            if product.price is not None and product.price != previous.price and extra.price is not None:
                product.price_at = extra.price_at  # WBCON: возраст неизвестен; BHAPI: data_freshness
                product.price_fetched_at = int(time.time())
                product.price_origin = "aggregator"
            product.used_fallback = previous.used_fallback or bool(
                product.title != previous.title or product.price != previous.price or product.image_urls != previous.image_urls
            )
            if not (product.title and product.price and product.image_urls):
                product = replace(product, import_note=" ".join(filter(None, (note, extra.import_note))))
            if not fields and not extra.import_note:
                product = replace(product, import_note=(product.import_note + f" {label} не вернул данные артикула.").strip())
            remember(product)
        output[index] = finish(product)
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
    product = from_page(Product(market, "", url), response)
    if product.title:
        product.title_source = "saved_html"
    return product


def record(product, mode):
    def price(value):
        return format(value, "f") if value is not None else None

    return {
        "marketplace": product.marketplace, "sku": sku_in_url(product.url), "url": product.url,
        "title": product.title, "title_source": product.title_source or None,
        "title_confirmed": bool(product.title) and not product.provisional_title,
        "image_urls": product.image_urls, "price": price(product.price), "currency": "RUB",
        "old_price": price(product.old_price), "price_note": product.price_note or None,
        "conditional_price": price(product.conditional_price), "conditional_note": product.conditional_note or None,
        "source": product.price_source or None, "mode": mode,
        "price_at": (datetime.fromtimestamp(product.price_at, timezone.utc).isoformat(timespec="seconds")
                     if product.price_at else None),
        "requested_size": httpx.URL(product.url).params.get("size"),
        "requested_wb_dest": (os.getenv("WB_DEST") or "-1257786") if product.marketplace == "wb" and mode == "live" else None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok" if product.price is not None else ("unavailable" if product.price_unavailable else "unknown"),
        "gaps": product.gaps or None,
        "reference": product.reference_note or None,
        "message": product.import_note or None,
    }


async def run(args):
    slots = asyncio.Semaphore(3)
    traces: dict[int, list] = {}

    async def collect(index, value):
        async with slots:
            try:
                if args.html:
                    return saved_page(value, args.html)
                with capture() as _lines:
                    product = await fetch_product(value, direct_only=True)
                traces[index] = list(product.trace)
                return product
            except (ParseError, OSError, UnicodeError, httpx.HTTPError, httpx.InvalidURL, asyncio.TimeoutError) as error:
                return {"input": value, "status": "error", "price": None, "message": str(error) or "Истекло время ожидания."}

    results = await asyncio.gather(*(collect(i, value) for i, value in enumerate(args.products)))
    if not args.html and not args.direct_only:
        results = await supplement_products(results)
    mode = "saved_html" if args.html else "live"
    rows = [record(p, mode) if isinstance(p, Product) else p for p in results]
    if args.diag:
        blocks = []
        for value, p in zip(args.products, results):
            if not isinstance(p, Product):
                blocks.append((value, None, False, []))
                print(f"{value}: {p['message']}\n")
                continue
            has_photo = False
            if p.image_urls and not args.html:
                # Как в боте: фото считается полученным, только если файл скачан и декодирован.
                try:
                    from photos import download_jpeg
                    with capture_events() as photo_events:
                        photo, _ = await asyncio.wait_for(download_jpeg(p), 18)
                    p.trace = list(p.trace) + photo_events
                    has_photo = photo is not None
                except (ImportError, asyncio.TimeoutError):
                    has_photo = False
            finish(p, has_photo)
            events = list(p.trace)
            print("\n".join(line.replace("<b>", "").replace("</b>", "") for line in
                            report.summary(p, has_photo, events, sku=sku_in_url(p.url))))
            for e in events:
                print("   · " + report.describe_event(e))
            print()
            blocks.append((value, p, has_photo, events))
        print("Для пересылки (без токенов и данных базы):")
        print(report.export(blocks))
    elif args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(row.get("title") or row.get("url") or row.get("input"))
            if row["price"] is None:
                print(row.get("message") or "Цена не получена.")
                if row.get("reference"):
                    print(row["reference"])
                continue
            caption = fmt_price(Decimal(row["price"]))
            print(caption + (" · " + row["price_note"] if row["price_note"] else ""))
            if row["conditional_price"] and row["conditional_price"] != row["price"]:
                print(f"{row['conditional_note']} · {fmt_price(Decimal(row['conditional_price']))}")
            print(f"Источник: {row['source']}")
    return 0 if all(row["status"] == "ok" for row in rows) else 1


def main():
    from dotenv import load_dotenv
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("products", nargs="+", help="Ссылки, wb:АРТИКУЛ или ozon:АРТИКУЛ")
    parser.add_argument("--direct-only", action="store_true", help="Запрашивать только площадки")
    parser.add_argument("--json", action="store_true", help="Результаты в JSON")
    parser.add_argument("--diag", action="store_true", help="Отчёт как /diag и строка для пересылки")
    parser.add_argument("--html", type=Path, metavar="ФАЙЛ", help="Разобрать сохранённую страницу UTF-8 офлайн")
    parser.add_argument("--no-memory", action="store_true", help="Не читать и не писать память бота в базе")
    args = parser.parse_args()
    if args.html and len(args.products) != 1:
        parser.error("--html используется для одного товара")
    database = here / "borzmarket.sqlite3"
    if not args.no_memory and database.exists():
        state.configure(database)  # общая с ботом пауза WBCON и возраст последней цены
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
