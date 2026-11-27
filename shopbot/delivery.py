"""Публикация карточки и обработка очереди в работающем боте."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, time as local_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from collage import post_caption, post_photo
from drafts import Draft, DraftStore
from history import Receipt
from outbound import outgoing_links, post_keyboard

log = logging.getLogger("shopbot.delivery")
try:
    MOSCOW = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    MOSCOW = timezone(timedelta(hours=3), "МСК")

SEND_UNKNOWN = "⚠️ Telegram не подтвердил отправку. Проверьте канал. Запись видна в /drafts."
SEND_REJECTED = (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
    TelegramRetryAfter, TelegramUnauthorizedError,
)


def format_time(timestamp: int, short: bool = False) -> str:
    pattern = "%d.%m %H:%M" if short else "%d.%m.%Y %H:%M МСК"
    return datetime.fromtimestamp(timestamp, MOSCOW).strftime(pattern)


def parse_time(text: str) -> int:
    moment = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW)
    timestamp = int(moment.timestamp())
    if timestamp <= int(time.time()):
        raise ValueError("Нужно указать будущее время")
    return timestamp


def queue_slots() -> tuple[local_time, ...]:
    raw = os.getenv("QUEUE_SLOTS", "10:00,13:00,16:00,19:00")
    slots = {datetime.strptime(part.strip(), "%H:%M").time() for part in raw.split(",") if part.strip()}
    if not slots:
        raise ValueError("QUEUE_SLOTS должен содержать хотя бы одно время по Москве.")
    return tuple(sorted(slots))


def future_slots(now: int, slots: tuple[local_time, ...]) -> Iterator[int]:
    day = datetime.fromtimestamp(now, MOSCOW).date()
    while True:
        for slot in slots:
            timestamp = int(datetime.combine(day, slot, tzinfo=MOSCOW).timestamp())
            if timestamp > now:
                yield timestamp
        day += timedelta(days=1)


def published_keyboard(post: Message) -> InlineKeyboardMarkup | None:
    url = post.get_url()
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть пост", url=url)
        ]])
    return None


async def publish(
    bot: Bot, store: DraftStore, draft: Draft, destination: int | str, *, avoid_repeat: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None]:
    originals, urls, tracked = outgoing_links(draft)
    if not store.claim(
        draft, destination, int(time.time()), urls=originals, tracked=tracked, avoid_repeat=avoid_repeat,
    ):
        if avoid_repeat:
            return "Карточка сохранена для проверки повтора. Откройте /drafts.", None
        return "Запись уже обработана. Откройте /drafts или /queue.", None

    try:
        photo = await asyncio.to_thread(post_photo, draft)
        if not photo:
            raise ValueError("no photo")
        caption = post_caption(draft, urls)
    except asyncio.CancelledError:
        store.finish(draft, draft.state, retry_at=draft.due_at)
        raise
    except Exception:
        reason = "Не удалось подготовить фото. Замените изображение в /drafts."
        store.finish(draft, "draft", reason)
        return f"⚠️ {reason}", None

    try:
        post = await asyncio.wait_for(
            bot.send_photo(
                destination, BufferedInputFile(photo, "borzmarket.jpg"),
                caption=caption, reply_markup=post_keyboard(draft, urls),
            ),
            timeout=75,
        )
    except TelegramRetryAfter as error:
        if draft.state == "queued":
            when = int(time.time()) + max(1, int(error.retry_after)) + 1
            store.finish(draft, "queued", retry_at=when)
            return f"Telegram попросил подождать. Новый срок отправки {format_time(when)}.", None
        reason = f"Telegram попросил подождать {error.retry_after} с. Карточка в /drafts."
        store.finish(draft, "draft", reason)
        return f"⚠️ {html.escape(reason)}", None
    except SEND_REJECTED as error:
        reason = f"Публикация отклонена. {str(error)[:170]}"
        store.finish(draft, "draft", reason)
        return f"⚠️ {html.escape(reason)} Карточка в /drafts.", None
    except asyncio.CancelledError:
        store.finish(draft, "uncertain", SEND_UNKNOWN)
        raise
    except (TelegramAPIError, asyncio.TimeoutError):
        store.finish(draft, "uncertain", SEND_UNKNOWN)
        return SEND_UNKNOWN, None
    except Exception as error:
        log.warning("Сбой во время отправки записи %s (%s)", draft.id, type(error).__name__)
        store.finish(draft, "uncertain", SEND_UNKNOWN)
        return SEND_UNKNOWN, None

    username = getattr(post.chat, "username", None)
    receipt = Receipt(
        post.chat.id, f"@{username.lower()}" if username else "",
        post.message_id, post.get_url() or "", int(post.date.timestamp()),
    )
    store.finish(draft, "sent", receipt=receipt)
    return "✅ Опубликовано в канале.", published_keyboard(post)


async def run_queue(bot: Bot, store: DraftStore) -> None:
    while True:
        try:
            now = int(time.time())
            store.recover_expired(now)
            draft = store.next_due(now)
            if draft is not None and draft.destination is not None:
                text, markup = await publish(bot, store, draft, draft.destination)
                try:
                    await bot.send_message(draft.chat_id, text, reply_markup=markup)
                except TelegramAPIError:
                    log.warning("Не удалось отправить уведомление о записи %s", draft.id)
                continue
        except Exception:
            log.exception("Ошибка при обработке очереди")
        await asyncio.sleep(5)
