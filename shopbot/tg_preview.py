"""Необязательно: превью ссылки, которое Telegram уже построил для сообщения администратора.

Bot API не передаёт боту содержимое превью: в Message есть только link_preview_options
(core.telegram.org/bots/api, прочитано 27.11.2026). MTProto-метод messages.getMessages
доступен ботам («Both users and bots can use this method»,
core.telegram.org/method/messages.getMessages) и возвращает сообщение целиком, включая
webPage: url, site_name, title, description, photo.

Так бот видит те же название и миниатюру, что и человек в чате, даже если Ozon закрыл страницу
для адреса телефона. Нужны:
  • TG_API_ID и TG_API_HASH — регистрация приложения на my.telegram.org владельцем бота;
  • библиотека telethon (requirements-preview.txt).
Вход выполняется токеном бота, не аккаунтом человека. Файл tg_preview.session хранит ключ
сессии: это секрет, его нельзя пересылать и класть в архивы.
Без ключей или библиотеки модуль ничего не делает.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from diagnostics import event

LOG = logging.getLogger("shopbot.tg_preview")
SESSION = Path(__file__).with_name("tg_preview.session")
WAIT = 6.0  # Telegram строит превью асинхронно: ждём не дольше этого

try:  # необязательная зависимость
    from telethon import TelegramClient, functions, types
except Exception:  # нет библиотеки — модуль выключен
    TelegramClient = functions = types = None

_client = None
_lock = asyncio.Lock()
_broken = ""


@dataclass
class Preview:
    url: str
    site_name: str
    title: str
    description: str
    photo: bytes | None


def credentials() -> tuple[int, str, str] | None:
    api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not (api_id.isdigit() and re.fullmatch(r"[0-9a-f]{32}", api_hash) and token):
        return None
    return int(api_id), api_hash, token


def enabled() -> bool:
    return TelegramClient is not None and credentials() is not None and not _broken


def describe() -> str:
    if TelegramClient is None:
        return "Превью Telegram · выключено (нет telethon)"
    if credentials() is None:
        return "Превью Telegram · выключено (нет TG_API_ID/TG_API_HASH)"
    if _broken:
        return f"Превью Telegram · сбой входа: {_broken}"
    return "Превью Telegram · включено"


async def _get_client():
    global _client, _broken
    async with _lock:
        if _client is None:
            api_id, api_hash, token = credentials()
            client = TelegramClient(str(SESSION), api_id, api_hash, receive_updates=False)
            try:
                await asyncio.wait_for(client.start(bot_token=token), 30)
            except Exception as error:  # неверные ключи, сеть, блокировка MTProto
                _broken = type(error).__name__
                await client.disconnect()
                raise
            _client = client
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None


def clean_title(title: str, site: str) -> str:
    """Убирает хвосты магазина: «… купить на OZON по низкой цене», «… – Wildberries»."""
    text = " ".join((title or "").split())
    text = re.split(r"\s+(?:купить|–|—|\|)\s+.*(?:ozon|wildberries|wb)\b.*$", text, flags=re.I)[0]
    text = re.sub(r"\s*\(\d{6,}\)\s*$", "", text)
    return text.strip(" -–—|")


def parse_webpage(page) -> Preview | None:
    """WebPage из MTProto → Preview без фото. Отдельно, чтобы проверять без сети."""
    url = getattr(page, "url", None)
    if not isinstance(url, str) or not url:
        return None
    site = getattr(page, "site_name", None) or ""
    title = clean_title(getattr(page, "title", None) or "", site)
    description = getattr(page, "description", None) or ""
    return Preview(url, site, title, description, None)


async def fetch(message_id: int) -> Preview | None:
    """Превью из сообщения администратора в личке бота. None — превью нет или модуль выключен."""
    if not enabled():
        return None
    started = time.perf_counter()
    try:
        client = await _get_client()
        deadline = time.monotonic() + WAIT
        while True:
            result = await client(functions.messages.GetMessagesRequest(id=[types.InputMessageID(message_id)]))
            message = next(iter(getattr(result, "messages", []) or []), None)
            media = getattr(message, "media", None)
            page = getattr(media, "webpage", None) if isinstance(media, types.MessageMediaWebPage) else None
            if isinstance(page, types.WebPagePending) and time.monotonic() < deadline:
                await asyncio.sleep(0.8)
                continue
            if not isinstance(page, types.WebPage):
                event("tg.preview", "empty", ms=int((time.perf_counter() - started) * 1000),
                      detail="Telegram не построил превью")
                return None
            preview = parse_webpage(page)
            if preview is None:
                return None
            if getattr(page, "photo", None) is not None:
                buffer = io.BytesIO()
                await asyncio.wait_for(client.download_media(page.photo, file=buffer), 20)
                preview.photo = buffer.getvalue() or None
            event("tg.preview", "ok", ms=int((time.perf_counter() - started) * 1000),
                  fields=tuple(name for name, value in (("название", preview.title), ("фото", preview.photo)) if value))
            return preview
    except Exception as error:
        LOG.warning("Превью Telegram недоступно (%s)", type(error).__name__)
        event("tg.preview", "error", ms=int((time.perf_counter() - started) * 1000), detail=type(error).__name__)
        return None
