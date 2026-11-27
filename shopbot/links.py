"""Проверка ссылок на товары и адресов, по которым бот делает запросы."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

import httpx

from card import ParseError
from diagnostics import step

MARKET_DOMAINS = {
    "ozon": ("ozon.ru", "ozon.by", "ozon.kz"),
    "wb": (
        "wb.ru", "wildberries.ru", "wildberries.by", "wildberries.kz",
        "wildberries.am", "wildberries.kg", "wildberries.uz",
    ),
}
IMAGE_DOMAINS = {
    "ozon": MARKET_DOMAINS["ozon"] + ("ozone.ru",),
    "wb": MARKET_DOMAINS["wb"] + ("wbbasket.ru",),
}
MAX_REDIRECTS = 5


class LinkError(ParseError):
    """Ссылка повреждена или ведёт за пределы разрешённых доменов."""


def _parse_url(value: str | httpx.URL) -> httpx.URL:
    try:
        return httpx.URL(value)
    except (ValueError, httpx.InvalidURL) as error:
        raise LinkError("Не удалось прочитать ссылку.") from error


def _matches_domain(url: httpx.URL, domains: tuple[str, ...]) -> bool:
    host = url.host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def allowed_url(value: str | httpx.URL, domains: tuple[str, ...]) -> httpx.URL:
    url = _parse_url(value)
    if (
        url.scheme != "https"
        or url.userinfo
        or url.port not in (None, 443)
        or not _matches_domain(url, domains)
    ):
        raise LinkError("Адрес за пределами разрешённых HTTPS-ссылок площадки.")
    return url


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_URL_TEXT = re.compile(r"https?://[^\s<>\[\]()]+", re.IGNORECASE)
# Ссылка, скопированная из адресной строки без https://
_BARE_URL = re.compile(
    r"(?<![\w@/.:-])((?:[a-z0-9-]+\.)*(?:ozon\.(?:ru|by|kz)|wildberries\.(?:ru|by|kz|am|kg|uz)|wb\.ru)/[^\s<>\[\]()]*)",
    re.IGNORECASE,
)


_LINK = re.compile(_URL_TEXT.pattern + "|" + _BARE_URL.pattern, re.IGNORECASE)
_ARTICLE = re.compile(
    r"(?:(wb|вб|wildberries|вайлдберриз|ozon|озон)[ \t]*[:#№]?[ \t]*)?([0-9]{1,16})",
    re.IGNORECASE,
)


def parse_article(text: str) -> tuple[str | None, str] | None:
    match = _ARTICLE.fullmatch(text.strip())
    if not match:
        return None
    label, sku = match.groups()
    if not int(sku) or (not label and len(sku) < 5):
        return None
    if not label:
        return None, sku
    return ("ozon" if label.casefold() in ("ozon", "озон") else "wb"), sku


def article_url(market: str, sku: str) -> str:
    if market == "ozon":
        return f"https://www.ozon.ru/product/{int(sku)}/"
    return f"https://www.wildberries.ru/catalog/{int(sku)}/detail.aspx"


def normalize_references(text: str) -> str:
    """Приводим ссылки и отдельные строки с артикулом к единому виду в исходном порядке."""
    lines = []
    for line in text.splitlines():
        article = parse_article(line)
        if article and article[0]:
            lines.append(article_url(*article))
        else:
            lines.append(_LINK.sub(lambda m: "https://" + m[0] if m[1] else m[0], line))
    return "\n".join(lines)


def extract_urls(text: str) -> list[str]:
    matches = _URL_TEXT.findall(normalize_references(text or ""))
    return list(dict.fromkeys(url.rstrip(").,;!?»\"'") for url in matches))


def _canonical(market: str, url: httpx.URL) -> httpx.URL:
    """Редкие формы ссылок приводим к обычным, дальше по коду их знать не нужно."""
    path = url.path
    if market == "wb":
        card = url.params.get("card")
        if path.rstrip("/").endswith("/product") and card and card.isascii() and card.isdigit():
            size = url.params.get("size")
            query = f"size={size}".encode() if size and size.isascii() and size.isdigit() else None
            return url.copy_with(path=f"/catalog/{int(card)}/detail.aspx", query=query, fragment=None)
        return url
    match = re.fullmatch(r"/context/detail/id/([0-9]{1,16})/?", path)
    if match:
        return url.copy_with(path=f"/product/{match.group(1)}/", query=None, fragment=None)
    return url


def product_link(value: str) -> tuple[str, httpx.URL]:
    value = value.strip()
    if value and not _SCHEME.match(value) and _BARE_URL.fullmatch(value):
        value = "https://" + value
    url = _parse_url(value)
    for market, domains in MARKET_DOMAINS.items():
        if _matches_domain(url, domains):
            if url.scheme == "http" and url.port is None:
                url = url.copy_with(scheme="https")
            return market, _canonical(market, allowed_url(url, domains))
    raise LinkError("Пришлите ссылку на товар Ozon или Wildberries.")


async def get_allowed(
    client: httpx.AsyncClient,
    value: str | httpx.URL,
    domains: tuple[str, ...],
    *,
    params: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    url = allowed_url(value, domains)
    for _ in range(MAX_REDIRECTS + 1):
        response = await client.get(url, params=params, headers=headers, follow_redirects=False)
        if not response.has_redirect_location:
            return response
        try:
            destination = response.url.join(response.headers["location"])
        except (ValueError, httpx.InvalidURL) as error:
            raise LinkError("Площадка вернула повреждённое перенаправление.") from error
        url = allowed_url(destination, domains)
        params = None
    raise LinkError("Площадка вернула слишком много перенаправлений.")


async def resolve_product_url(client, value, domains, is_product: Callable[[httpx.URL], bool], market):
    """Stop at the first product destination. The landing page may be unavailable."""
    async def walk():
        current = allowed_url(value, domains)
        for _ in range(MAX_REDIRECTS + 1):
            if is_product(current):
                return current
            response = await client.get(current, follow_redirects=False)
            step("Короткая ссылка " + market, f"HTTP {response.status_code}")
            if not response.has_redirect_location:
                if response.status_code >= 400:
                    raise ParseError(f"Короткая ссылка {market} — HTTP {response.status_code}. Пришлите прямую ссылку на товар.")
                if market == "Ozon" and current.path.startswith("/t/"):
                    raise ParseError("Ozon не раскрыл короткую ссылку. Можно прислать /ozon и артикул из приложения.")
                raise LinkError(f"В ссылке {market} не найден товар. Скопируйте прямую ссылку из карточки.")
            try:
                target = current.join(response.headers["location"])
            except (ValueError, httpx.InvalidURL) as error:
                raise LinkError("Не удалось прочитать адрес перенаправления.") from error
            current = allowed_url(target, domains)
        raise ParseError(f"{market} вернул слишком много перенаправлений. Пришлите прямую ссылку на товар.")
    try:
        return await asyncio.wait_for(walk(), 7)
    except (httpx.HTTPError, asyncio.TimeoutError) as error:
        raise ParseError(f"Не удалось открыть короткую ссылку {market}. Пришлите прямую ссылку на товар.") from error
