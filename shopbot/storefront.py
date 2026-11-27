"""Чтение открытых данных конкретной карточки из JSON-LD и HTML."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import httpx

from card import Product, parse_price
from diagnostics import step
from store_prices import widget_text
from store_prices import ozon_quote
from links import IMAGE_DOMAINS, LinkError, allowed_url, get_allowed

LOG = logging.getLogger("shopbot.storefront")
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "ru-RU,ru;q=0.9"}


TRANSIENT_STATUS = {500, 502, 503, 504}
BLOCK_MARKERS = (
    "<title>доступ ограничен", "id=\"challenge-form\"", "fab_chlg", "<title>проверка безопасности",
    "antibot challenge", "<title>access denied",
)


def _reason(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "тайм-аут"
    if isinstance(error, LinkError):
        return str(error).rstrip(".")
    return type(error).__name__


def retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After по RFC 9110: число секунд или HTTP-дата. Возвращает секунды ожидания."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit() and len(value) <= 7:
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    now = time.time() if now is None else now
    return max(0.0, moment.timestamp() - now)


async def read_source(client, url, domains, label, *, params=None, page=False, retries=1, timeout=6, info=None):
    """Один GET с одним повтором при тайм-ауте, обрыве связи и ответах 5xx.

    403, 429 и 404 не повторяются: повтор их не исправит и только злит площадку.
    В info (если передан словарь) попадают код ответа, Retry-After, попытки и длительность.
    """
    response = None
    started = time.perf_counter()
    info = info if info is not None else {}
    for attempt in range(retries + 1):
        last = attempt == retries
        info["attempts"] = attempt + 1
        try:
            response = await asyncio.wait_for(
                get_allowed(client, url, domains, params=params, headers=PAGE_HEADERS if page else None), timeout
            )
        except (httpx.HTTPError, LinkError, asyncio.TimeoutError) as error:
            reason = _reason(error)
            if not last and not isinstance(error, LinkError):
                await asyncio.sleep(0.4 + random.random() * 0.4)
                continue
            step(label, reason)
            LOG.info("%s: %s", label, reason)
            info.update(status=None, reason=reason, ms=int((time.perf_counter() - started) * 1000))
            return None, f"{label} — {reason}"
        if response.status_code in TRANSIENT_STATUS and not last:
            await asyncio.sleep(0.4 + random.random() * 0.4)
            continue
        break
    reason = f"HTTP {response.status_code}"
    sample = response.text[:6000].lower()
    blocked = any(marker in sample for marker in BLOCK_MARKERS)
    if blocked:
        reason += " (страница проверки доступа)"
    ms = int((time.perf_counter() - started) * 1000)
    step(label, f"{reason} · {ms / 1000:.1f} с".replace(".", ","))
    info.update(status=response.status_code, blocked=blocked, reason=reason,
                retry_after=retry_after(response.headers.get("retry-after")), ms=ms)
    if response.status_code != 200 or blocked:
        LOG.info("%s: %s", label, reason)
        return None, f"{label} — {reason}"
    return response, ""


def decode(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None
    return raw


def merged(first: Product, other: Product) -> Product:
    sold_out = first.price_unavailable or other.price_unavailable
    priced = first if first.price is not None else other
    if (first.conditional_price is not None and first.price_note and
            other.price_source == "ozon.webPrice" and other.price and not other.price_note):
        priced = other
    conditional = first if first.conditional_price is not None else other
    source = (first if first.price_unavailable else other).price_source if sold_out else priced.price_source
    return replace(first, title=first.title or other.title,
                   image_urls=list(dict.fromkeys(first.image_urls + other.image_urls))[:3],
                   price=None if sold_out else priced.price, old_price=None if sold_out else priced.old_price,
                   price_note="" if sold_out else priced.price_note, price_source=source, wb_dest=priced.wb_dest,
                   rating=first.rating or other.rating, feedbacks=first.feedbacks if first.rating else other.feedbacks,
                   import_note="" if priced.price is not None and not sold_out else first.import_note or other.import_note,
                   conditional_price=None if sold_out else conditional.conditional_price,
                   conditional_note="" if sold_out else conditional.conditional_note, price_unavailable=sold_out)


def images(product, values):
    urls = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("src") or value.get("contentUrl") or value.get("url")
        if not isinstance(value, str) or not value.strip():
            continue
        url = html.unescape(value.strip())
        if url.startswith("//"):
            url = "https:" + url
        try:
            url = str(allowed_url(url, IMAGE_DOMAINS[product.marketplace]))
        except LinkError:
            continue
        if url not in urls:
            urls.append(url)
        if len(urls) == 3:
            break
    return urls


def as_list(value):
    return value if isinstance(value, list) else [value]


def sku_in_url(value):
    path = str(value or "").split("?", 1)[0].split("#", 1)[0]
    match = re.search(r"/(?:catalog/(\d+)(?:/|$)|product/(?:[^/]+-)?(\d+)(?:/|$))", path)
    return next((part for part in match.groups() if part), "") if match else ""


def product_nodes(raw):
    pending = list(as_list(decode(raw)))
    while pending:
        part = pending.pop()
        if not isinstance(part, dict):
            continue
        types = {str(t).rsplit("/", 1)[-1] for t in as_list(part.get("@type"))}
        if "Product" in types:
            yield part
        if "ProductGroup" in types:
            pending.extend(as_list(part.get("hasVariant")))
        pending.extend(as_list(part.get("@graph")))
        pending.extend(as_list(part.get("mainEntity")))


def node_matches(node, sku):
    sku_field = str(node.get("sku") or "")
    ids = [sku_field] if sku_field.isascii() and sku_field.isdigit() else []
    ids.extend(filter(None, (sku_in_url(node.get("url")), sku_in_url(node.get("@id")))))
    return all(identifier == sku for identifier in ids) if ids else None


def in_rubles(currency):
    return isinstance(currency, str) and currency.strip().upper() in ("RUB", "RUR", "₽")


def valid_price_dates(offer):
    now = datetime.now(timezone(timedelta(hours=3)))
    for key in ("validFrom", "validThrough", "priceValidUntil"):
        if key not in offer:
            continue
        text = str(offer[key])
        try:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                boundary = datetime.fromisoformat(text).date()
                current = now.date()
            else:
                boundary = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if boundary.tzinfo is None:
                    boundary = boundary.replace(tzinfo=now.tzinfo)
                current = now
            if (key == "validFrom" and current < boundary) or (key != "validFrom" and current > boundary):
                return False
        except (ValueError, OverflowError):
            return False
    return True


def from_jsonld(base, documents):
    nodes = [node for document in documents for node in product_nodes(document)]
    sku = sku_in_url(base.url)
    matches = [node for node in nodes if node_matches(node, sku) is True]
    if not matches and len(nodes) == 1 and node_matches(nodes[0], sku) is None:
        matches = nodes
    result = base
    prices = set()
    availability = []
    saw_offer = False
    for node in matches:
        title = node.get("name")
        photo = images(base, as_list(node.get("image")))
        result = merged(result, Product(base.marketplace, title.strip() if isinstance(title, str) else "",
                                        base.url, image_urls=photo))
        for offer in as_list(node.get("offers")):
            if not isinstance(offer, dict) or node_matches(offer, sku) is False:
                continue
            saw_offer = True
            if not valid_price_dates(offer):
                continue
            if any(str(t).rsplit("/", 1)[-1] == "AggregateOffer" for t in as_list(offer.get("@type"))):
                continue
            state = str(offer.get("availability", "")).rstrip("/").rsplit("/", 1)[-1]
            unavailable = state in ("OutOfStock", "Discontinued", "SoldOut")
            availability.append(unavailable)
            if unavailable:
                continue
            amount = parse_price(offer.get("price")) if in_rubles(offer.get("priceCurrency")) else None
            if amount is not None:
                prices.add(amount)
                continue
            if offer.get("price") is not None:
                continue
            for spec in as_list(offer.get("priceSpecification")):
                if not isinstance(spec, dict) or any(k in spec for k in (
                    "priceType", "validForMemberTier", "eligibleQuantity", "billingDuration", "referenceQuantity"
                )):
                    continue
                if str(spec.get("@type", "PriceSpecification")).rsplit("/", 1)[-1] not in ("PriceSpecification", "UnitPriceSpecification"):
                    continue
                if valid_price_dates(spec) and in_rubles(spec.get("priceCurrency") or offer.get("priceCurrency")):
                    if amount := parse_price(spec.get("price")):
                        prices.add(amount)
    source = base.marketplace + ".jsonld"
    if availability and all(availability):
        return replace(result, price_unavailable=True, price_source=source)
    if len(prices) == 1:
        return replace(result, price=prices.pop(), price_source=source,
                       price_note="цена в карточке" if base.marketplace == "ozon" else "")
    if saw_offer:
        return replace(result, import_note="В разметке карточки нет однозначной действующей цены в рублях.")
    return result


def from_widget(base, name, widget):
    if not isinstance(widget, dict):
        return base
    for key in ("sku", "skuId"):
        value = str(widget.get(key) or "")
        if value.isascii() and value.isdigit() and value != sku_in_url(base.url):
            return base
    if name == "webPrice":
        return ozon_quote(base, widget)
    if name in ("webProductHeading", "webProductTitle"):
        title = widget_text(widget.get("title") or widget.get("heading"))
        return replace(base, title=title)
    if name == "webGallery":
        values = [widget.get("coverImage"), *as_list(widget.get("images"))]
        return replace(base, image_urls=images(base, values))
    return base


class PageMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.documents = []
        self.ozon_states = []
        self.h1 = []
        self.gallery = []
        self.scope = []
        self.script = None
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and a.get("data-state"):
            name = (a.get("id") or "").removeprefix("state-").split("-", 1)[0]
            if name in ("webPrice", "webGallery", "webProductHeading", "webProductTitle"):
                self.ozon_states.append((name, decode(a["data-state"])))
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content"):
                self.meta.setdefault(key.lower(), a["content"])
        if tag == "link" and "canonical" in (a.get("rel") or "").lower().split():
            self.canonical = a.get("href") or ""
        if tag == "script" and (a.get("type") or "").lower().split(";")[0].strip() == "application/ld+json":
            self.script = []
        prev = self.scope[-1][1] if self.scope else ""
        scope = "gallery" if a.get("data-widget") == "webGallery" else ("h1" if tag == "h1" else prev)
        if tag == "img" and scope == "gallery":
            self.gallery.append(a.get("data-src") or a.get("src"))
        if tag not in ("area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"):
            self.scope.append((tag, scope))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.documents.append("".join(self.script))
            self.script = None
        if self.scope and self.scope[-1][0] == tag:
            self.scope.pop()

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.scope and self.scope[-1][1] == "h1":
            self.h1.append(data)


def from_page(base: Product, response: httpx.Response) -> Product:
    sku = sku_in_url(base.url)
    if not sku or sku_in_url(response.url) != sku:
        return base
    parser = PageMarkup()
    parser.feed(response.text)
    parser.close()
    meta = parser.meta
    for permalink in (parser.canonical, meta.get("og:url")):
        if permalink and sku_in_url(permalink) != sku:
            return base
    candidates = [node for doc in parser.documents for node in product_nodes(doc)]
    if candidates and all(node_matches(node, sku) is False for node in candidates):
        return base
    node_result = from_jsonld(base, parser.documents)
    product_type = meta.get("og:type", "").lower() in ("product", "og:product", "product.item")
    if base.marketplace == "ozon" and parser.ozon_states:
        # The final URL already identifies the SKU. Canonical and explicit SKU conflicts were rejected above.
        widget_result = base
        for name, widget in parser.ozon_states:
            widget_result = merged(widget_result, from_widget(base, name, widget))
        node_result = merged(widget_result, node_result)
    confirmed_url = sku_in_url(parser.canonical or meta.get("og:url")) == sku
    if not (product_type or node_result.title or parser.gallery or
            (confirmed_url and (parser.h1 or meta.get("og:title")) and meta.get("og:image"))):
        return node_result
    title = " ".join(" ".join(parser.h1).split()) or meta.get("og:title", "").strip()
    amount = None
    for key in ("product:price", "og:price"):
        if not node_result.import_note and in_rubles(meta.get(key + ":currency")):
            amount = parse_price(meta.get(key + ":amount"))
            if amount:
                break
    photo = images(base, [*parser.gallery, meta.get("og:image:secure_url"), meta.get("og:image")])
    return merged(node_result, Product(base.marketplace, title, base.url, amount, image_urls=photo,
                                       price_note="цена в карточке" if amount and base.marketplace == "ozon" else "",
                                       price_source=base.marketplace + ".opengraph" if amount else ""))
