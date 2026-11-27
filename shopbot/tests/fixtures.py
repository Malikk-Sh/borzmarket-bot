"""Обезличенные ответы площадок для тестов v10. Без сети и без Telegram-токена.

Структура страниц повторяет живые ответы 27.11.2026 (JSON-LD Product, виджеты webPrice,
webGallery, webProductHeading, canonical), значения — для двух публичных товаров из ТЗ.
"""
from __future__ import annotations

import html
import io
import json
import re
from collections import Counter
from decimal import Decimal

import httpx
from PIL import Image

OZ_SKU = "1526882900"
OZ_SLUG = "ryukzak-muzhskoy-gorodskoy-dlya-noutbuka-s-usb-transformer-ruchnaya-klad-dlya-komandirovok-dlya"
OZ_TITLE = "Рюкзак мужской городской для ноутбука с USB, трансформер, ручная кладь, 30 л, LUGGARIKA"
STEAM_SKU = "6307910379"
STEAM_SLUG = "paroochistitel-dlya-uborki-doma-moshchnyy-parogenerator-professionalnyy-15000-vt-15-bar-15-nasadok"
SHORT = "https://ozon.ru/t/377XHKT"
WB_NM = "866768707"


def jpeg(size=(60, 80), color=(90, 120, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def ozon_page(sku=OZ_SKU, slug=OZ_SLUG, title=OZ_TITLE, *, price="2 969 ₽", card="2 672 ₽", old="14 976 ₽",
              available=True, widgets=True, ld_price="2969", ld_availability="InStock", valid_until=None,
              canonical_sku=None):
    canonical = f"https://www.ozon.ru/product/{slug}-{canonical_sku or sku}/"
    offer = {"@type": "Offer", "availability": f"https://schema.org/{ld_availability}", "price": ld_price,
             "priceCurrency": "RUB"}
    if valid_until:
        offer["priceValidUntil"] = valid_until
    ld = {"@context": "https://schema.org", "@type": "Product", "sku": canonical_sku or sku, "name": title,
          "image": [f"https://ir-20.ozone.ru/s3/multimedia-1-k/{sku}1.jpg",
                    f"https://ir-20.ozone.ru/s3/multimedia-1-k/{sku}2.jpg"], "offers": offer}
    states = ""
    if widgets:
        price_widget = {"isAvailable": available, "showOriginalPrice": True, "originalPrice": old,
                        "lexemes": {"withOzonCard": "С банками", "withoutOzonCard": "С другими банками"}}
        if price:
            price_widget["price"] = price
        if card:
            price_widget["cardPrice"] = card
        for name, data in (("webPrice", price_widget), ("webProductHeading", {"title": title}),
                           ("webGallery", {"sku": sku, "images": [{"src": f"https://ir-20.ozone.ru/s3/multimedia-1-k/{sku}1.jpg"}]})):
            states += (f'<div id="state-{name}-1-default-1" data-state=\'{html.escape(json.dumps(data, ensure_ascii=False), quote=False)}\'></div>')
    return (f'<html><head><title>{title} купить на OZON ({sku})</title><link rel="canonical" href="{canonical}">'
            f'<meta property="og:title" content="{html.escape(title)}"><meta property="og:url" content="{canonical}">'
            f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></head>'
            f'<body><h1>{html.escape(title)}</h1>{states}</body></html>')


CHALLENGE = "<html><head><title>Antibot Challenge Page</title></head><body>fab_chlg</body></html>"


def wb_row(nm=WB_NM, price=4113300, name="Робот-пылесос", qty=5):
    return {"id": int(nm), "name": name, "totalQuantity": qty,
            "sizes": [{"optionId": 1, "name": "", "price": {"basic": price * 2, "product": price}, "stocks": [{"qty": qty}]}]}


class World:
    """Маршрутизатор ответов. routes: список (функция-условие, ответ или функция)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = Counter()
        self.log = []

    async def __call__(self, request):
        for check, answer in self.routes:
            if check(request):
                self.calls[check.__name__] += 1
                self.log.append(f"{request.method} {request.url.host}{request.url.path}")
                result = answer(request) if callable(answer) else answer
                if isinstance(result, Exception):
                    raise result
                return result
        self.calls["unmatched"] += 1
        self.log.append(f"? {request.url}")
        return httpx.Response(404)

    def transport(self):
        return httpx.MockTransport(self)


def ozon_short(r):
    return r.url.host in ("ozon.ru", "www.ozon.ru") and r.url.path.startswith("/t/")


def ozon_product(r):
    return r.url.host == "www.ozon.ru" and r.url.path.startswith("/product/")


def ozon_api(r):
    return r.url.host == "www.ozon.ru" and r.url.path.startswith("/api/")


def ozon_image(r):
    return r.url.host.endswith("ozone.ru")


def wbcon_ozon(r):
    return r.url.host == "oz.wbcon.su"


def wbcon_wb(r):
    return r.url.host == "prices.wbcon.su"


def wb_detail(r):
    return r.url.host == "card.wb.ru"


def wb_basket_card(r):
    return r.url.host.endswith("wbbasket.ru") and r.url.path.endswith("card.json")


def wb_history(r):
    return r.url.host.endswith("wbbasket.ru") and r.url.path.endswith("price-history.json")


def wb_image(r):
    return r.url.host.endswith("wbbasket.ru") and "/images/" in r.url.path


def wb_page(r):
    return r.url.host == "www.wildberries.ru" and r.url.path.startswith("/catalog/")


def redirect_to(sku=STEAM_SKU, slug=STEAM_SLUG):
    return httpx.Response(301, headers={"Location": f"https://www.ozon.ru/product/{slug}-{sku}/?from=share_android"})


def sku_of(request) -> str:
    match = re.search(r"(\d{5,})/?$", request.url.path)
    return match.group(1) if match else ""


def decimal(value) -> Decimal:
    return Decimal(str(value))
