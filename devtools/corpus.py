"""Офлайн-корпус BorzMarket: одинаковые ответы площадок для разных версий бота.

Каждый сценарий описывает «мир» — что отвечают Ozon, WB, WBCON и BHAPI на уровне HTTP —
и эталон: какое название, цену и фото должен получить бот. Сетевой слой подменяется
через httpx.MockTransport, поэтому код версии работает без изменений.

    python devtools/corpus.py --bot-dir путь/к/shopbot --out result.json

Задержки ответов имитируют живые замеры 27.11.2026 из облака (страница Ozon ~1,2 с).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import html
import io
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

LAT = {  # секунды, имитация
    "ozon_short": 0.6, "ozon_page": 1.2, "ozon_api": 0.8, "ozon_main": 0.6, "ozon_img": 0.25,
    "wb_detail": 0.5, "wb_internal": 0.5, "wb_page": 0.6, "wb_basket": 0.15, "wb_history": 0.15,
    "wb_img": 0.25, "wbcon_wb": 1.0, "wbcon_ozon": 1.5, "bhapi": 4.0, "other": 0.2,
}
SCALE = float(os.getenv("CORPUS_LATENCY_SCALE", "1"))

# --- эталонные товары -----------------------------------------------------------------
# Названия и цены Ozon сняты с публичных страниц 27.11.2026 (облако), остальное — вымысел.
OZON = {
    "1526882900": dict(
        slug="ryukzak-muzhskoy-gorodskoy-dlya-noutbuka-s-usb-transformer-ruchnaya-klad-dlya-komandirovok-dlya",
        title="Рюкзак мужской городской для ноутбука с USB, трансформер, ручная кладь, для командировок, для путешествий, 30 л, LUGGARIKA",
        price=Decimal("2969"), old=Decimal("14976"), card=Decimal("2672"), card_label="С банками",
        regular_label="С другими банками", images=["https://ir-20.ozone.ru/s3/multimedia-1-k/8053543280.jpg",
                                                  "https://ir-20.ozone.ru/s3/multimedia-1-8/11709294452.jpg"]),
    "6307910379": dict(
        slug="paroochistitel-dlya-uborki-doma-moshchnyy-parogenerator-professionalnyy-15000-vt-15-bar-15-nasadok",
        title="Пароочиститель для уборки дома мощный / Парогенератор профессиональный 15000 Вт, 15 Бар / 15 насадок",
        price=Decimal("4009"), old=Decimal("25000"), card=Decimal("3608"), card_label="С банками",
        regular_label="С другими банками", images=["https://ir-20.ozone.ru/s3/multimedia-1-j/20119550671.jpg"]),
    "1000000101": dict(
        slug="termos-stalnoy-1-l", title="Термос стальной 1 л", price=Decimal("1299"), old=Decimal("2590"),
        card=None, card_label="", regular_label="", images=["https://ir-20.ozone.ru/s3/multimedia-1-a/1000000101.jpg"]),
}
SHORT = {"377XHKT": "6307910379"}
WB = {
    "866768707": dict(title="Робот-пылесос с влажной уборкой и лидаром", price=Decimal("41133"),
                      basic=Decimal("89990"), sizes=[("0", "", Decimal("41133"), 7)], photos=3),
    "1421304340": dict(title="Берцы мужские тактические зимние", price=Decimal("4890"), basic=Decimal("9900"),
                       sizes=[("101", "42", Decimal("4890"), 3), ("102", "43", Decimal("5190"), 2),
                              ("103", "44", Decimal("4990"), 0)], photos=4),
    "14671602": dict(title="Термос вакуумный 500 мл", price=Decimal("1299"), basic=Decimal("2598"),
                     sizes=[("0", "", Decimal("1299"), 5)], photos=2),
}


def ozon_widgets(sku, product, *, card_only=False, sold_out=False, no_widgets=False):
    if no_widgets:
        return {}
    price = {"isAvailable": not sold_out, "showOriginalPrice": True,
             "originalPrice": f"{product['old']:,} ₽".replace(",", " "),
             "lexemes": {"withOzonCard": product["card_label"] or "с Ozon Картой",
                         "withoutOzonCard": product["regular_label"] or "без Ozon Карты"}}
    if not card_only:
        price["price"] = f"{product['price']:,} ₽".replace(",", " ")
    if product["card"] is not None or card_only:
        price["cardPrice"] = f"{(product['card'] or product['price']):,} ₽".replace(",", " ")
    return {
        "webPrice": price,
        "webProductHeading": {"title": product["title"]},
        "webGallery": {"sku": sku, "coverImage": product["images"][0],
                       "images": [{"src": u} for u in product["images"]]},
    }


def ozon_jsonld(sku, product, *, sold_out=False, stale=False, card_only=False):
    offer = {"@type": "Offer", "url": f"https://www.ozon.ru/product/{product['slug']}-{sku}/",
             "availability": "https://schema.org/" + ("OutOfStock" if sold_out else "InStock"),
             "price": str(product["card"] if card_only and product["card"] else product["price"]),
             "priceCurrency": "RUB"}
    if stale:
        offer["priceValidUntil"] = "2026-01-01"
    return {"@context": "https://schema.org", "@type": "Product", "sku": sku, "name": product["title"],
            "@id": f"https://www.ozon.ru/product/{product['slug']}-{sku}/#product-{sku}",
            "image": product["images"], "offers": offer}


def ozon_page(sku, product, **flags):
    canonical = f"https://www.ozon.ru/product/{product['slug']}-{sku}/"
    widgets = ozon_widgets(sku, product, card_only=flags.get("card_only", False),
                           sold_out=flags.get("sold_out", False), no_widgets=flags.get("no_widgets", False))
    ld = ozon_jsonld(sku, product, sold_out=flags.get("sold_out", False), stale=flags.get("stale", False),
                     card_only=flags.get("card_only", False))
    states = "".join(
        f'<div id="state-{name}-1-default-1" data-state=\'{html.escape(json.dumps(data, ensure_ascii=False), quote=False)}\'></div>'
        for name, data in widgets.items())
    return (f'<html><head><title>{html.escape(product["title"])} купить на OZON ({sku})</title>'
            f'<link rel="canonical" href="{canonical}">'
            f'<meta property="og:title" content="{html.escape(product["title"])}">'
            f'<meta property="og:image" content="{product["images"][0]}">'
            f'<meta property="og:url" content="{canonical}">'
            f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
            f'</head><body><h1>{html.escape(product["title"])}</h1>{states}</body></html>')


def ozon_api(sku, product, **flags):
    widgets = ozon_widgets(sku, product, card_only=flags.get("card_only", False),
                           sold_out=flags.get("sold_out", False), no_widgets=flags.get("no_widgets", False))
    ld = ozon_jsonld(sku, product, sold_out=flags.get("sold_out", False), stale=flags.get("stale", False),
                     card_only=flags.get("card_only", False))
    return {"widgetStates": {f"{name}-1-default-1": json.dumps(data, ensure_ascii=False) for name, data in widgets.items()},
            "seo": {"title": product["title"], "script": [{"innerHTML": json.dumps(ld, ensure_ascii=False)}]}}


def wb_detail_row(nm, product, *, sold_out=False):
    sizes = []
    for option, name, price, qty in product["sizes"]:
        sizes.append({"optionId": int(option), "name": name, "origName": name,
                      "price": {"basic": int(product["basic"] * 100), "product": int(price * 100),
                                "total": int(price * 100)},
                      "stocks": [{"qty": 0 if sold_out else qty}]})
    return {"id": int(nm), "name": product["title"], "sizes": sizes,
            "totalQuantity": 0 if sold_out else sum(s[3] for s in product["sizes"])}


def image_bytes(fmt="JPEG"):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (60, 80), (90, 120, 200)).save(buffer, fmt)
    return buffer.getvalue()


JPEG = None
WEBP = None


def predicted_basket(vol):
    ranges = json.loads((BOT_DIR / "wb_media_ranges.json").read_text(encoding="utf-8"))
    for first, last, host in ranges:
        if first <= vol <= last:
            return int(re.search(r"basket-(\d+)", host).group(1))
    return int(re.search(r"basket-(\d+)", ranges[-1][2]).group(1))


# --- мир сценария ------------------------------------------------------------------------
class World:
    def __init__(self, spec):
        self.spec = spec
        self.calls = Counter()
        self.hits = Counter()  # для «сначала тайм-аут, потом ответ»
        self.log = []

    def category(self, request):
        host, path = request.url.host, request.url.path
        if host in ("ozon.ru", "www.ozon.ru") and path.startswith("/t/"):
            return "ozon_short"
        if host == "www.ozon.ru" and path.startswith("/api/"):
            return "ozon_api"
        if host == "www.ozon.ru" and path.startswith("/product/"):
            return "ozon_page"
        if host == "www.ozon.ru" and path in ("/", ""):
            return "ozon_main"
        if host.endswith("ozone.ru"):
            return "ozon_img"
        if host == "card.wb.ru":
            return "wb_detail"
        if host == "www.wildberries.ru" and path.startswith("/__internal/"):
            return "wb_internal"
        if host == "www.wildberries.ru":
            return "wb_page"
        if host.endswith("wbbasket.ru"):
            if path.endswith("card.json"):
                return "wb_basket"
            if path.endswith("price-history.json"):
                return "wb_history"
            return "wb_img"
        if host == "prices.wbcon.su":
            return "wbcon_wb"
        if host == "oz.wbcon.su":
            return "wbcon_ozon"
        if host == "bhapi.ru":
            return "bhapi"
        return "other"

    async def __call__(self, request):
        import httpx
        cat = self.category(request)
        self.calls[cat] += 1
        await asyncio.sleep(LAT.get(cat, 0.2) * SCALE)
        try:
            response = self.respond(cat, request)
        except httpx.TimeoutException:
            self.log.append((cat, "timeout"))
            raise
        self.log.append((cat, response.status_code))
        return response

    def respond(self, cat, request):
        import httpx
        s = self.spec
        flags = s.get("flags", {})
        if cat in flags.get("timeout_once", ()) and self.hits[cat] == 0:
            self.hits[cat] += 1
            raise httpx.ReadTimeout("имитация тайм-аута")
        self.hits[cat] += 1
        if cat == "ozon_short":
            code = request.url.path.split("/")[2]
            mode = flags.get("short", "ok")
            if mode == "403" or (mode == "bare403" and request.url.host == "ozon.ru"):
                return httpx.Response(403, text="<html><title>Antibot Challenge Page</title></html>")
            sku = SHORT[code]
            p = OZON[sku]
            return httpx.Response(301, headers={"Location": f"https://www.ozon.ru/product/{p['slug']}-{sku}/?from=share_android&short={code}"})
        if cat in ("ozon_page", "ozon_api", "ozon_main"):
            mode = flags.get("ozon", "ok")
            if mode == "403":
                return httpx.Response(403, text="<html><title>Antibot Challenge Page</title><div id=\"challenge-form\"></div></html>")
            if mode == "challenge200" and cat != "ozon_main":
                return httpx.Response(200, text="<html><head><title>Antibot Challenge Page</title></head><body>fab_chlg</body></html>")
            if cat == "ozon_main":
                return httpx.Response(200, text="<html><title>OZON</title></html>")
            if cat == "ozon_page":
                sku = re.search(r"(\d+)/?$", request.url.path).group(1)
            else:
                sku = re.search(r"(\d+)/?$", request.url.params.get("url", "").rstrip("/") + "/").group(1) \
                    if re.search(r"(\d+)/?$", request.url.params.get("url", "")) else ""
            if flags.get("substitute"):
                sku = flags["substitute"]
            product = OZON.get(sku)
            if product is None:
                return httpx.Response(404)
            if cat == "ozon_page":
                return httpx.Response(200, text=ozon_page(sku, product, **flags), headers={"content-type": "text/html"})
            return httpx.Response(200, json=ozon_api(sku, product, **flags))
        if cat == "ozon_img":
            if flags.get("img404"):
                return httpx.Response(404)
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if cat == "wbcon_ozon":
            mode = flags.get("wbcon", "ok")
            if mode == "429":
                return httpx.Response(429, headers={"Retry-After": "60"}, json={"detail": "Too Many Requests"})
            body = json.loads(request.content or b"{}")
            sku = str(body.get("article"))
            product = OZON.get(sku)
            if not product:
                return httpx.Response(200, json={"article": sku, "price": 0})
            return httpx.Response(200, json={"article": sku, "name": product["title"], "imageUrl": product["images"][0],
                                             "price": int(product["price"])})
        if cat == "wbcon_wb":
            mode = flags.get("wbcon", "ok")
            if mode == "429":
                return httpx.Response(429, headers={"Retry-After": "60"}, json={"detail": "Too Many Requests"})
            rows = []
            for nm in request.url.params.get("articles", "").split(";"):
                if nm in WB:
                    rows.append({"id": int(nm), "priceU": int(WB[nm]["basic"] * 100),
                                 "salePriceU": int(WB[nm]["price"] * 100), "supplier": "ООО", "supplierId": 1})
            return httpx.Response(200, json=rows)
        if cat == "bhapi":
            return httpx.Response(401, json={"status": "error"})
        if cat == "wb_detail":
            mode = flags.get("wb_detail", "ok")
            if mode == "403":
                return httpx.Response(403, headers={"server": "wbaas"})
            nms = request.url.params.get("nm", "").split(";")
            rows = []
            for nm in nms:
                target = flags.get("substitute") or nm
                if target in WB:
                    rows.append(wb_detail_row(target, WB[target], sold_out=flags.get("sold_out", False)))
            return httpx.Response(200, json={"data": {"products": rows}})
        if cat in ("wb_internal", "wb_page"):
            return httpx.Response(498, text="<html>wbaas challenge</html>")
        if cat in ("wb_basket", "wb_history", "wb_img"):
            n = int(re.search(r"basket-(\d+)", request.url.host).group(1))
            nm = int(re.search(r"/(\d+)/(?:info|images)/", request.url.path).group(1))
            true = predicted_basket(nm // 100000) + flags.get("basket_shift", 0)
            if n != true:
                return httpx.Response(404)
            product = WB.get(str(nm))
            if cat == "wb_basket":
                return httpx.Response(200, json={"nm_id": nm, "imt_name": product["title"],
                                                 "media": {"photo_count": product["photos"]}})
            if cat == "wb_history":
                stamp = int(time.time()) - 3 * 86400
                return httpx.Response(200, json=[{"dt": stamp, "price": {"RUB": int(product["price"] * 100) - 500}}])
            if flags.get("big404") and "/images/big/" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, content=WEBP, headers={"content-type": "image/webp"})
        return httpx.Response(404)


# --- сценарии ------------------------------------------------------------------------------
def oz(sku):
    p = OZON[sku]
    return dict(title=p["title"], price=p["price"], photo=True, market="ozon")


def wb(nm, price=None):
    p = WB[nm]
    return dict(title=p["title"], price=price if price is not None else p["price"], photo=True, market="wb")


SCENARIOS = [
    # Ozon
    dict(id="oz_ok_sku", input="ozon:1526882900", truth=oz("1526882900")),
    dict(id="oz_ok_short", input="https://ozon.ru/t/377XHKT", truth=oz("6307910379")),
    dict(id="oz_blocked_wbcon_ok", input="ozon:1526882900", flags={"ozon": "403"}, truth=oz("1526882900")),
    dict(id="oz_blocked_wbcon_429", input="ozon:1526882900", flags={"ozon": "403", "wbcon": "429"},
         truth=oz("1526882900")),
    dict(id="oz_short_blocked_429", input="https://ozon.ru/t/377XHKT", flags={"ozon": "403", "wbcon": "429"},
         truth=oz("6307910379")),
    dict(id="oz_short_flaky", input="https://ozon.ru/t/377XHKT", flags={"short": "bare403"}, truth=oz("6307910379")),
    dict(id="oz_challenge_200", input="ozon:1526882900", flags={"ozon": "challenge200", "wbcon": "429"},
         truth=oz("1526882900")),
    dict(id="oz_substituted", input="ozon:1526882900", flags={"substitute": "1000000101", "wbcon": "429"},
         truth=oz("1526882900")),
    dict(id="oz_card_only", input="ozon:1526882900", flags={"card_only": True},
         truth=dict(oz("1526882900"), price=Decimal("2672"), conditional=True)),
    dict(id="oz_sold_out", input="ozon:1526882900", flags={"sold_out": True},
         truth=dict(oz("1526882900"), price=None, unavailable=True)),
    dict(id="oz_stale_jsonld", input="ozon:1526882900", flags={"no_widgets": True, "stale": True, "wbcon": "429"},
         truth=dict(oz("1526882900"), price=None)),
    dict(id="oz_photo_404", input="ozon:1526882900", flags={"img404": True, "wbcon": "429"},
         truth=dict(oz("1526882900"), photo=False)),
    dict(id="oz_timeout_then_ok", input="ozon:1526882900", flags={"timeout_once": ("ozon_api", "ozon_page")},
         truth=oz("1526882900")),
    dict(id="oz_jsonld_only", input="ozon:1526882900", flags={"no_widgets": True}, truth=oz("1526882900")),
    # Wildberries
    dict(id="wb_ok", input="wb:866768707", truth=wb("866768707")),
    dict(id="wb_detail403_wbcon_ok", input="wb:866768707", flags={"wb_detail": "403"}, truth=wb("866768707")),
    dict(id="wb_detail403_wbcon_429", input="wb:866768707", flags={"wb_detail": "403", "wbcon": "429"},
         truth=wb("866768707")),
    dict(id="wb_size_link", input="https://www.wildberries.ru/catalog/1421304340/detail.aspx?size=102",
         truth=dict(wb("1421304340", Decimal("5190")), size="102")),
    dict(id="wb_multi_size_min", input="https://www.wildberries.ru/catalog/1421304340/detail.aspx",
         truth=wb("1421304340", Decimal("4890"))),
    dict(id="wb_sold_out", input="wb:14671602", flags={"sold_out": True},
         truth=dict(wb("14671602"), price=None, unavailable=True)),
    dict(id="wb_substituted", input="wb:14671602", flags={"substitute": "866768707", "wbcon": "429"},
         truth=wb("14671602")),
    dict(id="wb_basket_shift", input="wb:14671602", flags={"basket_shift": 2}, truth=wb("14671602")),
    dict(id="wb_big_photo_404", input="wb:14671602", flags={"big404": True}, truth=wb("14671602")),
    dict(id="wb_size_detail403", input="https://www.wildberries.ru/catalog/1421304340/detail.aspx?size=102",
         flags={"wb_detail": "403"}, truth=dict(wb("1421304340", Decimal("5190")), size="102")),
    dict(id="wb_timeout_then_ok", input="wb:866768707", flags={"timeout_once": ("wb_detail",)}, truth=wb("866768707")),
    dict(id="wb_history_only", input="wb:866768707", flags={"wb_detail": "403", "wbcon": "429"},
         truth=wb("866768707")),
]

COLLECTIONS = [
    dict(id="col6_ok", inputs=["ozon:1526882900", "https://ozon.ru/t/377XHKT", "ozon:1000000101",
                               "wb:866768707", "https://www.wildberries.ru/catalog/1421304340/detail.aspx", "wb:14671602"],
         truths=[oz("1526882900"), oz("6307910379"), oz("1000000101"), wb("866768707"),
                 wb("1421304340", Decimal("4890")), wb("14671602")]),
    dict(id="col6_blocked", inputs=["ozon:1526882900", "https://ozon.ru/t/377XHKT", "ozon:1000000101",
                                    "wb:866768707", "https://www.wildberries.ru/catalog/1421304340/detail.aspx", "wb:14671602"],
         flags={"ozon": "403", "wb_detail": "403"},
         truths=[oz("1526882900"), oz("6307910379"), oz("1000000101"), wb("866768707"),
                 wb("1421304340", Decimal("4890")), wb("14671602")]),
]


# --- запуск --------------------------------------------------------------------------------
BOT_DIR = Path(".")


def evaluate(item, truth):
    product = item.product
    title_ok = bool(product.title) and product.title.strip() == truth["title"]
    title_got = bool(product.title.strip())
    expected = truth.get("price")
    price_got = product.price is not None
    if expected is None:
        price_ok = product.price is None
    elif truth.get("conditional"):
        price_ok = product.price == expected and bool(product.price_note)
    else:
        price_ok = product.price == expected and not (product.price_note and product.conditional_price == product.price)
    photo_got = bool(item.photo)
    photo_ok = photo_got == truth.get("photo", True)
    wrong = []
    if title_got and not title_ok and getattr(product, "title_source", "") not in ("ozon.slug",):
        wrong.append("title")
    if price_got and not price_ok:
        wrong.append("price")
    provisional_title = getattr(product, "title_source", "") == "ozon.slug"
    ready = not item.missing
    return dict(title_ok=title_ok, title_got=title_got, provisional_title=provisional_title,
                price_ok=price_ok and (expected is not None), price_expected=expected is not None,
                price_got=price_got, photo_ok=photo_ok and photo_got, photo_got=photo_got,
                all_ok=title_ok and price_ok and photo_ok and expected is not None and photo_got,
                wrong=wrong, ready=ready, false_ready=ready and bool(wrong),
                source=getattr(product, "price_source", ""), note=getattr(product, "import_note", "")[:160])


async def run_one(bot, prices_mod, inputs, world, *, warm=False):
    import httpx
    original_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(world)
        kwargs.pop("proxy", None)
        kwargs.pop("proxies", None)
        original_init(self, *args, **kwargs)
    httpx.AsyncClient.__init__ = patched
    try:
        links = []
        for value in inputs:
            market, url = prices_mod.market_url(value)
            links.append((market, httpx.URL(url)))
        slots = asyncio.Semaphore(2)
        started = time.perf_counter()
        responses = await bot.retrieve_links(links, slots)
        elapsed = time.perf_counter() - started
    finally:
        httpx.AsyncClient.__init__ = original_init
    return responses, elapsed


def reset(bot_modules):
    state = sys.modules.get("state")
    if state is not None and hasattr(state, "reset"):
        state.reset()
    for name in ("prices", "wb", "wbcon", "net"):
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr in ("_RESULT_CACHE", "_DETAIL_CACHE"):
            cache = getattr(module, attr, None)
            if cache is not None:
                cache.clear()
        for attr in ("_cache", "_basket_cache", "_inflight"):
            value = getattr(module, attr, None)
            if hasattr(value, "clear"):
                value.clear()
        for attr in ("_recent",):
            value = getattr(module, attr, None)
            if isinstance(value, dict):
                for window in value.values():
                    window.clear()
        for attr in ("_blocked_until", "_cooldown"):
            value = getattr(module, attr, None)
            if isinstance(value, dict):
                for key in value:
                    value[key] = 0.0
        if hasattr(module, "reset_state"):
            module.reset_state()


async def main_async(args):
    global JPEG, WEBP, BOT_DIR
    BOT_DIR = args.bot_dir.resolve()
    sys.path.insert(0, str(BOT_DIR))
    os.environ.update({"HTTP_IMPERSONATE": "0", "WB_PRICE_FALLBACK": "1", "OZON_PRICE_FALLBACK": "1",
                       "BHAPI_OZON_TOKEN": "", "BHAPI_WB_TOKEN": "", "WB_DEST": "-1257786",
                       "BORZ_STATE_DB": ":memory:"})
    JPEG, WEBP = image_bytes("JPEG"), image_bytes("WEBP")
    import bot  # noqa
    import prices  # noqa
    results = {"singles": [], "collections": [], "warm": []}
    for spec in SCENARIOS:
        reset(None)
        world = World(spec)
        responses, elapsed = await run_one(bot, prices, [spec["input"]], world)
        response = responses[0]
        if isinstance(response, BaseException):
            row = dict(id=spec["id"], market=spec["truth"]["market"], error=f"{type(response).__name__}: {response}"[:200],
                       calls=dict(world.calls), seconds=round(elapsed, 2))
            row.update(dict(title_ok=False, title_got=False, price_ok=False, price_got=False, photo_ok=False,
                            photo_got=False, all_ok=False, wrong=[], ready=False, false_ready=False,
                            price_expected=spec["truth"].get("price") is not None, provisional_title=False))
        else:
            item, warning = response
            row = dict(id=spec["id"], market=spec["truth"]["market"], calls=dict(world.calls), seconds=round(elapsed, 2),
                       warning=warning[:300], **evaluate(item, spec["truth"]))
            row["title"] = item.product.title[:80]
            row["price"] = str(item.product.price) if item.product.price is not None else None
        results["singles"].append(row)
        print(f"{spec['id']:24} {row.get('seconds'):>5}s calls={sum(world.calls.values()):>3} "
              f"T={'+' if row['title_ok'] else ('~' if row.get('provisional_title') else ('?' if row['title_got'] else '-'))} "
              f"P={'+' if row['price_ok'] else ('!' if 'price' in row['wrong'] else '-')} "
              f"F={'+' if row['photo_ok'] else '-'} ready={row['ready']} wrong={row['wrong']} {row.get('error','')}")
    # повторный (тёплый) импорт: тот же процесс, тот же мир
    for spec in [s for s in SCENARIOS if s["id"] in ("oz_ok_sku", "wb_ok", "wb_detail403_wbcon_ok", "oz_blocked_wbcon_ok")]:
        reset(None)
        world = World(spec)
        _, cold = await run_one(bot, prices, [spec["input"]], world)
        cold_calls = sum(world.calls.values())
        world.calls.clear()
        responses, warm = await run_one(bot, prices, [spec["input"]], world)
        results["warm"].append(dict(id=spec["id"], cold_s=round(cold, 2), warm_s=round(warm, 2),
                                    cold_calls=cold_calls, warm_calls=sum(world.calls.values()),
                                    warm_calls_by=dict(world.calls)))
        print(f"warm {spec['id']:22} cold {cold:.2f}s/{cold_calls} calls · warm {warm:.2f}s/{sum(world.calls.values())} calls")
    for spec in COLLECTIONS:
        reset(None)
        world = World({"flags": spec.get("flags", {})})
        responses, elapsed = await run_one(bot, prices, spec["inputs"], world)
        rows = []
        for response, truth in zip(responses, spec["truths"]):
            if isinstance(response, BaseException):
                rows.append(dict(error=type(response).__name__))
                continue
            rows.append(evaluate(response[0], truth))
        results["collections"].append(dict(id=spec["id"], seconds=round(elapsed, 2), calls=dict(world.calls),
                                           total_calls=sum(world.calls.values()),
                                           all_ok=sum(r.get("all_ok", False) for r in rows),
                                           false_ready=sum(r.get("false_ready", False) for r in rows), items=rows))
        print(f"{spec['id']:24} {elapsed:.2f}s calls={sum(world.calls.values())} by={dict(world.calls)} "
              f"all_ok={sum(r.get('all_ok', False) for r in rows)}/6")
    summary = {}
    for market in ("ozon", "wb"):
        rows = [r for r in results["singles"] if r["market"] == market]
        priced = [r for r in rows if r["price_expected"]]
        summary[market] = dict(
            n=len(rows), title_ok=sum(r["title_ok"] for r in rows), title_got=sum(r["title_got"] for r in rows),
            title_provisional=sum(r.get("provisional_title", False) for r in rows),
            price_n=len(priced), price_ok=sum(r["price_ok"] for r in priced), price_got=sum(r["price_got"] for r in rows),
            photo_ok=sum(r["photo_ok"] for r in rows), all_ok=sum(r["all_ok"] for r in rows),
            wrong_values=sum(len(r["wrong"]) for r in rows), false_ready=sum(r["false_ready"] for r in rows),
            calls=sum(sum(r["calls"].values()) for r in rows),
            paid_calls=sum(r["calls"].get("bhapi", 0) for r in rows),
            seconds_median=round(statistics.median(r["seconds"] for r in rows), 2),
            seconds_max=round(max(r["seconds"] for r in rows), 2))
    results["summary"] = summary
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
