"""Партнёрские ссылки и необязательный счётчик переходов."""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qsl

import httpx
from aiohttp import web
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from card import Product, fmt_price
from drafts import Draft, DraftStore
from history import click_token
from links import MARKET_DOMAINS, LinkError, allowed_url


def click_base() -> str:
    raw = os.getenv("CLICK_BASE_URL", "").strip()
    if not raw:
        return ""
    url = httpx.URL(raw)
    if (
        url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
        or url.path != "/"
    ):
        raise ValueError("CLICK_BASE_URL должен быть публичным HTTPS-адресом без пути, параметров и логина.")
    return str(url).rstrip("/")


def affiliate_params(market: str) -> dict[str, str]:
    source = os.getenv(f"{market.upper()}_AFFILIATE_PARAMS", "").strip().lstrip("?")
    return {
        name: value for name, value in parse_qsl(source, keep_blank_values=True, max_num_fields=40)
        if re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name)
        and name.lower() not in {"size", "sku", "offerid", "chrtid"}
    }


def outbound_url(product: Product, erid: str) -> str:
    url = allowed_url(product.url, MARKET_DOMAINS[product.marketplace])
    extra = affiliate_params(product.marketplace)
    if erid:
        extra["erid"] = erid
    return str(url.copy_merge_params(extra))


def outgoing_links(draft: Draft) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    originals = tuple(outbound_url(item.product, draft.erid) for item in draft.items)
    base = click_base()
    if not base:
        return originals, originals, False
    links = tuple(f"{base}/go/{click_token(draft.id, index)}" for index in range(len(originals)))
    return originals, links, True


def reaction_button(key: str, total: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=f"🔥 {total}", callback_data=f"bm:react:{key}")


def reaction_keyboard(key: str, total: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[reaction_button(key, total)]])


def post_keyboard(draft: Draft, links: tuple[str, ...], total: int = 0) -> InlineKeyboardMarkup:
    """Кнопки под постом: «купить» и счётчик 🔥. В подборке по кнопке на товар: «1 · 1 299 ₽»."""
    reaction = reaction_button(draft.id, total)
    products = [item.product for item in draft.items]
    if len(products) == 1:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🛒 Открыть на {products[0].market_name}", url=links[0]), reaction,
        ]])

    buttons = [
        InlineKeyboardButton(text=f"{number} · {fmt_price(product.price) if product.price else 'открыть'}", url=url)
        for number, (product, url) in enumerate(zip(products, links), 1)
    ]
    per_row = 3 if len(buttons) in (3, 5, 6) else 2
    rows = [buttons[start:start + per_row] for start in range(0, len(buttons), per_row)]
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [reaction]])


def refresh_reaction(markup: InlineKeyboardMarkup | None, key: str, total: int) -> InlineKeyboardMarkup:
    """Обновляет счётчик 🔥 и не трогает кнопки покупки."""
    data = f"bm:react:{key}"
    rows, found = [], False
    for row in markup.inline_keyboard if markup else ():
        updated = []
        for button in row:
            if button.callback_data == data:
                button, found = reaction_button(key, total), True
            updated.append(button)
        rows.append(updated)
    if not found:
        rows.append([reaction_button(key, total)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def click_app(store: DraftStore) -> web.Application:
    app = web.Application()

    async def open_product(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        if not re.fullmatch(r"[a-f0-9]{16}-[1-6]", token):
            raise web.HTTPNotFound()
        item = store.history.click_destination(token)
        if item is None:
            raise web.HTTPNotFound()
        try:
            url = allowed_url(item["outbound_url"], MARKET_DOMAINS[item["marketplace"]])
        except (LinkError, KeyError):
            raise web.HTTPNotFound() from None

        agent = request.headers.get("User-Agent", "").lower()
        purpose = " ".join(request.headers.get(name, "") for name in ("Purpose", "Sec-Purpose")).lower()
        if "telegrambot" not in agent and "prefetch" not in purpose:
            store.history.count_click(token)
        raise web.HTTPFound(str(url), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    app.router.add_get("/go/{token}", open_product, allow_head=False)
    return app


async def start_clicks(store: DraftStore) -> web.AppRunner | None:
    for market in MARKET_DOMAINS:
        affiliate_params(market)
    if not click_base():
        return None

    runner = web.AppRunner(click_app(store), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(
            runner, os.getenv("CLICK_BIND_HOST", "127.0.0.1"), int(os.getenv("CLICK_PORT", "8088")),
        )
        await site.start()
    except (ValueError, OSError):
        await runner.cleanup()
        raise
    return runner
