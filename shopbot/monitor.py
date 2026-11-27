"""Фоновая проверка цен уже опубликованных товаров."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from card import fmt_price
from drafts import Draft, DraftItem, DraftStore
from history import Watch, product_key
from links import LinkError

log = logging.getLogger("shopbot.monitor")

FetchItem = Callable[[str, str, asyncio.Semaphore], Awaitable[tuple[DraftItem, str]]]
ShowDraft = Callable[[Bot, DraftStore, Draft], Awaitable[None]]


def fetch_result(item: DraftItem | None, warning: str = "") -> str:
    status = re.search(r"\bHTTP (403|429)\b", warning)
    if status:
        return status.group(1)
    if item is None or (warning and len(item.missing) == 3):
        return "error"
    return "partial" if item.missing else "ok"


def price_threshold() -> Decimal:
    try:
        threshold = Decimal(os.getenv("PRICE_DROP_PERCENT", "15"))
    except InvalidOperation as error:
        raise ValueError("PRICE_DROP_PERCENT должен быть числом.") from error
    if not threshold.is_finite() or not 0 < threshold < 100:
        raise ValueError("PRICE_DROP_PERCENT должен быть числом от 0 до 100 без крайних значений.")
    return threshold


async def check_price(
    bot: Bot, store: DraftStore, watch: Watch, threshold: Decimal,
    fetch: FetchItem, preview: ShowDraft, slots: asyncio.Semaphore,
) -> Draft | None:
    product = watch.product
    try:
        item, warning = await fetch(product.marketplace, product.url, slots)
    except LinkError as error:
        store.history.record_fetches(int(time.time()), [(product.marketplace, fetch_result(None, str(error)))])
        return None

    store.history.record_fetches(int(time.time()), [(product.marketplace, fetch_result(item, warning))])
    current = item.product
    if (
        current.price is None or current.price <= 0
        or current.price_note.casefold() != product.price_note.casefold()
        or product_key(current, variant=True) != product_key(product, variant=True)
    ):
        return None

    if current.marketplace == "wb" and current.wb_dest != product.wb_dest:
        if current.wb_dest:
            store.history.reset_wb_reference(watch, current)
        return None

    drop = (watch.reference - current.price) * 100 / watch.reference
    if drop < threshold:
        return None

    updated = replace(current, title=product.title or current.title)
    photo = item.photo or watch.photo
    final_item = DraftItem(updated, photo, item.photo_index if item.photo else None)
    draft = store.price_alert(watch, final_item)
    if draft is None:
        return None

    text = (
        f"Цена снизилась на {drop:.1f}%\n"
        f"{html.escape(updated.title)}\n"
        f"{fmt_price(watch.reference)} → <b>{fmt_price(current.price)}</b>\n"
        "Черновик сохранён в /drafts."
    )
    if watch.post_url:
        text += f'\n<a href="{html.escape(watch.post_url, quote=True)}">Прошлый пост</a>'
    try:
        await bot.send_message(watch.chat_id, text, disable_web_page_preview=True)
        await preview(bot, store, draft)
    except TelegramAPIError:
        log.warning("Не удалось показать черновик со снижением цены %s", draft.id)
    return draft


async def run_price_watch(
    bot: Bot, store: DraftStore, admins: set[int], slots: asyncio.Semaphore,
    fetch: FetchItem, preview: ShowDraft,
) -> None:
    threshold = price_threshold()
    while True:
        try:
            watch = store.history.claim_watch(int(time.time()))
            if watch is not None:
                if watch.owner_id in admins:
                    await check_price(bot, store, watch, threshold, fetch, preview, slots)
                await asyncio.sleep(2)
                continue
        except Exception as error:
            log.warning("Сбой проверки цен (%s)", type(error).__name__)
        await asyncio.sleep(30)
