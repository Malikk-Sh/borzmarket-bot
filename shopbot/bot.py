"""Карточки и подборки BorzMarket в Telegram."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import secrets
import time
from contextlib import suppress
from functools import partial
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from PIL import Image, ImageOps

from card import ParseError, Product, build_caption, fmt_price, manual_price, parse_price, short_text, shorten_name
from collage import LAYOUT_NAMES, caption_units, post_caption, post_photo
from delivery import MOSCOW, SEND_UNKNOWN, format_time, future_slots, parse_time, publish, queue_slots, run_queue
from drafts import MAX_ITEMS, Draft, DraftItem, DraftStore
from links import (IMAGE_DOMAINS, LinkError, article_url, extract_urls, get_allowed,
                   normalize_references, parse_article, product_link)
import net
from diagnostics import capture, step
from screenshot_price import recognize
from hints import fields as message_fields
from prices import fetch_product, market_url, saved_markup, supplement_products
from ozon import fetch_ozon
from monitor import fetch_result, price_threshold, run_price_watch
from outbound import refresh_reaction, start_clicks
from stats import PERIODS, render_history, render_stats
import ui
from history import product_key
from wb import fetch_wb, prefetch_many
import wbcon

load_dotenv()
log = logging.getLogger("shopbot")
router = Router()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
_channel = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_ID: int | str | None = (
    int(_channel) if _channel.lstrip("-").isdigit() else (_channel or None)
)
AUTO_POST = os.getenv("AUTO_POST", "0") == "1"
DB_PATH = Path(__file__).with_name("borzmarket.sqlite3")

FETCHERS = {"wb": fetch_wb, "ozon": fetch_ozon}
PAGE_SIZE = 6
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
RUBRICS = ("#дом", "#техника", "#одежда", "#скидка")
REACTION_LOCK = asyncio.Lock()
IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

PROMPTS = {
    "title": "✏️ Пришлите название товара.",
    "prices": "🧮 Укажите номер товара и цену на отдельных строках, например\n<code>1 = 1 299,90\n3 = 590</code>\nОстальные цены сохранятся.",
    "append": "＋ Пришлите ссылки на товары для этого черновика. Можно прислать число и выбрать площадку или написать <code>wb 866768707</code> или <code>ozon 1526882900</code>.",
    "price": "💰 Пришлите цену в рублях, например 1 299,90. Знак «-» очищает цену в черновике.",
    "photo": "🖼 Пришлите фото или изображение файлом до 10 МБ.",
    "screenshot": "📷 Пришлите скриншот с ценой и знаком ₽. Можно обрезать его до блока цен, затем выберите обычную цену без условий оплаты.",
    "quick": "⚡ Пришлите «Термос 500 мл — 1 299 ₽». Можно приложить фото и написать это в подписи. Для одной цены введите только сумму.",
    "heading": "📝 Пришлите заголовок подборки.",
    "comment": "💬 Пришлите короткий комментарий к посту. Знак «-» удаляет его.",
    "erid": "🏷 Пришлите готовый erid длиной до 64 знаков. Знак «-» очищает поле.",
}


@dataclass(frozen=True)
class Input:
    key: str
    revision: int
    field: str
    index: int = 0
    choices: tuple[Decimal, ...] = ()
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))


INPUTS: dict[tuple[int, int], Input] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def button(text: str, *data: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="bm:" + ":".join(map(str, data)))


def action(text: str, name: str, draft: Draft, *extra: object) -> InlineKeyboardButton:
    return button(text, name, draft.id, draft.revision, *extra)


def menu_button(queue: bool, offset: int = 0, *, back: bool = False) -> InlineKeyboardButton:
    label = ("‹ Очередь" if queue else "‹ Черновики") if back else ("🕓 Очередь" if queue else "📝 Черновики")
    return button(label, "list", "q" if queue else "d", offset)


def home_button() -> InlineKeyboardButton:
    return button("🏠 Меню", "home")


def main_menu(drafts: int = 0, queued: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("📝 Черновики" + (f" · {drafts}" if drafts else ""), "list", "d", 0),
         button("🕓 Очередь" + (f" · {queued}" if queued else ""), "list", "q", 0)],
        [button("📊 Статистика", "stats", 7), button("📚 История", "history")],
        [button("🩺 Диагностика", "diagmenu"), button("❓ Помощь", "help")],
    ])


NAVIGATION = ui.REPLY_ROWS


def navigation_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in NAVIGATION],
        resize_keyboard=True, is_persistent=True,
    )


def cancel_keyboard(request: Input) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("✖ Отмена", "cancel", request.nonce)]])


def item_buttons(draft: Draft, index: int, *, more: bool = False) -> list[list[InlineKeyboardButton]]:
    item = draft.items[index]
    product = item.product
    photos = len(product.image_urls)
    rows = []
    if more:
        rows.append([action("📷 Цена со скриншота", "edit", draft, "screenshot", index)])
        if wbcon.available(product):
            rows.append([action("🌐 Проверить через WBCON", "external", draft, index)])
        rows.append([action("🩺 Диагностика", "diag", draft, index)])
        if product.title and shorten_name(product.title) != product.title:
            rows.append([action("✂️ Сократить название", "shorten", draft, index)])
        rows.append([action("‹ К товару", "item", draft, index)])
        return rows
    if item.missing:
        rows.append([action("↻ Загрузить недостающее", "retry", draft, index)])
        rows.append([action("⚡ Быстрый ввод", "edit", draft, "quick", index)])
    if wbcon.available(product) and (not product.price or (product.marketplace == "ozon" and item.missing)):
        rows.append([action("🌐 Проверить через WBCON", "external", draft, index)])
    rows.append([action("💰 " + ("Ввести цену" if not product.price else "Цена"), "edit", draft, "price", index),
                 action("✏️ " + ("Ввести название" if not product.title else "Название"), "edit", draft, "title", index)])
    photo_row = [action("🖼 " + ("Загрузить фото" if not item.photo else "Заменить фото"), "edit", draft, "photo", index)]
    if photos > 1 or (photos and item.photo_index is None):
        position = "открыть" if item.photo_index is None else f"{item.photo_index + 1}/{photos}"
        photo_row.append(action(f"🎞 Галерея · {position}", "gallery", draft, index))
    elif not photos and item.photo:
        photo_row.append(action("🔎 Найти фото", "gallery", draft, index))
    rows.append(photo_row)
    rows.append([action("🛠 Ещё", "more", draft, index)])
    if len(draft.items) > 1:
        order = []
        if index:
            order.append(action("↑ Раньше", "up", draft, index))
        if index + 1 < len(draft.items):
            order.append(action("↓ Позже", "down", draft, index))
        order.append(action("🗑 Убрать", "remove", draft, index))
        rows.append(order)
    return rows


def preview_keyboard(draft: Draft) -> InlineKeyboardMarkup:
    if draft.state == "queued":
        rows = [
            [action("🚀 В канал сейчас", "post", draft)],
            [action("🕓 Перенести", "time", draft), action("↩️ В черновики", "unschedule", draft)],
            [menu_button(True, back=True), home_button()],
        ]
    elif draft.state == "sending":
        rows = [[menu_button(True, back=True), home_button()]]
    elif draft.state == "uncertain":
        rows = [[action("🗑 Удалить запись", "drop", draft)], [menu_button(False, back=True), home_button()]]
    else:
        rows = []
        many = len(draft.items) > 1
        if draft.ready:
            rows.append([action("🚀 Опубликовать", "post", draft), action("🕓 Запланировать", "time", draft)])
        if many:
            chips = [action(f"✏️ {i + 1}" + (" ⚠️" if item.missing else ""), "item", draft, i)
                     for i, item in enumerate(draft.items)]
            per_row = 4 if len(chips) <= 4 else 3
            rows.extend(chips[start:start + per_row] for start in range(0, len(chips), per_row))
        elif draft.ready:
            rows.append([action("✏️ Изменить", "item", draft, 0)])
        else:
            rows.extend(item_buttons(draft, 0))
        manage = []
        if len(draft.items) < MAX_ITEMS:
            manage.append(action("＋ Товар", "edit", draft, "append", 0))
        if many:
            manage.append(action("🧮 Цены", "edit", draft, "prices", 0))
        manage.append(action("🎨 Стиль", "design", draft))
        if not many and draft.ready:
            rows[-1].extend(manage)
        else:
            rows.append(manage)
        rows.append([menu_button(False, back=True), action("🗑 Удалить", "drop", draft)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview_text(draft: Draft) -> str:
    notes = ""
    if draft.state == "sending":
        notes = "\n\n📤 Отправляю в канал…"
    elif draft.state == "queued" and draft.due_at is not None:
        notes = f"\n\n🕓 Запланировано на <b>{format_time(draft.due_at)}</b>"
    elif draft.state == "uncertain":
        notes = "\n\n⚠️ " + html.escape(draft.error or SEND_UNKNOWN)
    else:
        notes = ui.status_notes(draft.items, ready=draft.state == "draft" and draft.ready, many=len(draft.items) > 1)
        if draft.error:
            notes += "\n\n⚠️ " + html.escape(short_text(draft.error, 135))
    return post_caption(draft, reserve=caption_units(notes)) + notes


def convert_to_jpeg(raw: bytes) -> bytes:
    with Image.open(io.BytesIO(raw)) as source:
        with ImageOps.exif_transpose(source).convert("RGB") as img:
            img.thumbnail((1280, 1280))
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def image_variants(url: str) -> list[str]:
    """Если крупный снимок WB недоступен, пробуем средний размер того же кадра."""
    match = re.fullmatch(r"(https://basket-\d+\.wbbasket\.ru/vol\d+/part\d+/\d+/images/)big(/\d+\.webp)", url)
    return [url, match[1] + "c516x688" + match[2]] if match else [url]


async def download_jpeg(
    product: Product, indexes: tuple[int, ...] | None = None
) -> tuple[bytes | None, int | None]:
    if indexes is None:
        indexes = tuple(range(len(product.image_urls)))
    if not indexes:
        return None, None
    proxy = os.getenv(f"{product.marketplace.upper()}_PROXY") or None
    try:
        async with httpx.AsyncClient(headers=IMAGE_HEADERS, timeout=15, proxy=proxy) as client:
            for index in indexes:
                value = product.image_urls[index]
                url = "https:" + value if value.startswith("//") else value
                for variant in image_variants(url):
                    try:
                        response = await get_allowed(client, variant, IMAGE_DOMAINS[product.marketplace])
                        step(f"Фото {index+1}", f"HTTP {response.status_code}")
                        if response.status_code == 200 and response.content:
                            return await asyncio.to_thread(convert_to_jpeg, response.content), index
                    except (LinkError, httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError) as error:
                        step(f"Фото {index+1}", type(error).__name__)
                        continue
    except (ImportError, ValueError, httpx.HTTPError) as error:
        log.warning("Не удалось открыть соединение для фото (%s)", type(error).__name__)
    return None, None


async def retrieve_item(
    market: str, url: str, slots: asyncio.Semaphore, *, need_photo: bool = True, direct_only: bool = False,
    allow_paid: bool = True, fresh: bool = False,
) -> tuple[DraftItem, str]:
    async with slots:
        try:
            product = await asyncio.wait_for(
                fetch_product(url, direct_only=direct_only, allow_paid=allow_paid, fresh=fresh),
                timeout=110 if not direct_only else 55,
            )
        except LinkError:
            raise
        except ParseError as error:
            return DraftItem(Product(market, "", url)), str(error)
        except asyncio.TimeoutError:
            return DraftItem(Product(market, "", url)), "Витрина ответила слишком поздно."
        except Exception as error:
            log.warning("Не удалось разобрать ответ %s (%s)", market, type(error).__name__)
            return DraftItem(Product(market, "", url)), "Витрина не отдала данные в ожидаемом формате."

        if not need_photo:
            return DraftItem(product), product.import_note
        try:
            photo, index = await asyncio.wait_for(download_jpeg(product), timeout=18)
        except asyncio.TimeoutError:
            photo, index = None, None
        note = product.import_note
        if product.image_urls and not photo:
            note = (note + " Фото не удалось скачать.").strip()
        return DraftItem(product, photo, index), note


async def retrieve_links(links, slots, *, fresh=False):
    wb_links = [str(url) for market, url in links if market == "wb"]
    if len(wb_links) > 1:
        async with slots:
            await prefetch_many(wb_links)  # одна заявка вместо шести
    responses = await asyncio.gather(
        *(retrieve_item(market, str(url), slots, direct_only=True, **({"fresh": True} if fresh else {}))
          for market, url in links),
        return_exceptions=True,
    )
    positions = [i for i, response in enumerate(responses) if isinstance(response, tuple)]
    # Для резерва учитываем скачанный файл, а не только наличие URL в галерее.
    candidates = []
    for position in positions:
        item, _ = responses[position]
        product = item.product
        candidates.append(replace(product, image_urls=[]) if not item.photo and product.image_urls else product)
    async with slots:
        updated = await supplement_products(candidates)
    for i, product in zip(positions, updated):
        item, warning = responses[i]
        if item.product is product:
            continue
        extra = warning.removeprefix(item.product.import_note).strip()
        photo, index = item.photo, item.photo_index
        if not photo:
            old_urls = item.product.image_urls
            new_urls = [u for u in product.image_urls if u not in old_urls]
            product = replace(product, image_urls=list(dict.fromkeys(new_urls + old_urls))[:3])
            if new_urls:
                async with slots:
                    try:
                        photo, index = await asyncio.wait_for(download_jpeg(product, tuple(range(min(len(new_urls), 3)))), 18)
                    except asyncio.TimeoutError:
                        pass
                extra = "" if photo else "Фото не удалось скачать."
        responses[i] = (DraftItem(product, photo, index), " ".join(filter(None, (product.import_note, extra))))
    return responses


async def clear_view(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is not None:
        with suppress(TelegramAPIError):
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )


async def send_view(
    bot: Bot,
    store: DraftStore,
    draft: Draft,
    text: str,
    markup: InlineKeyboardMarkup,
    photo: bytes | None = None,
) -> bool:
    message = None
    if photo:
        try:
            message = await bot.send_photo(
                draft.chat_id, BufferedInputFile(photo, "borzmarket.jpg"),
                caption=text, reply_markup=markup,
            )
        except TelegramBadRequest:
            pass
    if message is None:
        message = await bot.send_message(
            draft.chat_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW
        )

    bound = store.bind_view(draft, message.message_id)
    await clear_view(bot, draft.chat_id, draft.view_id if bound else message.message_id)
    return bound


async def show_preview(bot: Bot, store: DraftStore, draft: Draft) -> None:
    try:
        photo = await asyncio.to_thread(post_photo, draft)
    except (OSError, ValueError, Image.DecompressionBombError):
        photo = None
    await send_view(bot, store, draft, preview_text(draft), preview_keyboard(draft), photo)


async def show_item(bot: Bot, store: DraftStore, draft: Draft, index: int, *, more: bool = False) -> None:
    item = draft.items[index]
    many = len(draft.items) > 1
    p = item.product
    head = f"<b>{ui.MARKET_ICON.get(p.marketplace, '')} Товар {index + 1} из {len(draft.items)}</b>" if many else (
        f"<b>{ui.MARKET_ICON.get(p.marketplace, '')} Товар · {p.market_name}</b>")
    title = head + "\n\n" + build_caption(p)
    if p.conditional_price and p.conditional_price != p.price:
        title += f"\n{html.escape(p.conditional_note)} · {fmt_price(p.conditional_price)}"
    if item.missing:
        title += "\n\n✏️ Дополните: " + ", ".join(item.missing)
    if more:
        title += "\n\n🛠 Дополнительные действия"
    rows = item_buttons(draft, index, more=more) + [[button("‹ К черновику", "open", draft.id), home_button()]]
    await send_view(bot, store, draft, title, InlineKeyboardMarkup(inline_keyboard=rows), item.photo)


async def show_design(bot: Bot, store: DraftStore, draft: Draft) -> None:
    rows = []
    if len(draft.items) > 1:
        rows.append([action("📝 Заголовок подборки", "edit", draft, "heading", 0)])
        names = LAYOUT_NAMES.get(len(draft.items))
        if names:
            rows.append([action("🧩 Макет → «" + names[1 if draft.layout == "auto" else 0] + "»", "layout", draft)])
    rows.extend([
        [action("💬 Комментарий", "edit", draft, "comment", 0), action("#️⃣ Рубрики", "tags", draft)],
        [action("🏷 Рекламная маркировка · erid", "edit", draft, "erid", 0)],
        [button("‹ К черновику", "open", draft.id)],
    ])
    await send_view(bot, store, draft, "<b>🎨 Оформление поста</b>\nЗаголовок, макет коллажа, подпись и рубрики.",
                    InlineKeyboardMarkup(inline_keyboard=rows))


async def show_tags(bot: Bot, store: DraftStore, draft: Draft) -> None:
    rows = [
        [action(("☑️ " if tag in draft.tags else "⬜ ") + tag, "tag", draft, index)]
        for index, tag in enumerate(RUBRICS)
    ]
    rows.append([button("‹ К черновику", "open", draft.id)])
    await send_view(
        bot, store, draft, "<b>#️⃣ Рубрики поста</b>\nОтметьте нужные.", InlineKeyboardMarkup(inline_keyboard=rows)
    )


async def refresh(bot: Bot, store: DraftStore, old: Draft, item_index: int | None = None) -> None:
    await clear_view(bot, old.chat_id, old.view_id)
    updated = store.get(old.id)
    if updated:
        try:
            if item_index is not None and len(updated.items) > 1:
                await show_item(bot, store, updated, item_index)
            else:
                await show_preview(bot, store, updated)
        except TelegramAPIError:
            with suppress(TelegramAPIError):
                await bot.send_message(old.chat_id, "Изменение сохранено. Откройте /drafts или /queue.")


async def send_list(
    bot: Bot, store: DraftStore, user_id: int, chat_id: int, queue: bool, offset: int = 0
) -> None:
    entries = store.page(user_id, chat_id, queue, offset, PAGE_SIZE + 1)
    if offset and not entries:
        offset = 0
        entries = store.page(user_id, chat_id, queue, offset, PAGE_SIZE + 1)

    rows = []
    for entry in entries[:PAGE_SIZE]:
        when = format_time(entry.due_at, short=True) if queue and entry.due_at is not None else ""
        rows.append([button(ui.list_label(entry, queue=queue, when=when), "open", entry.id)])

    navigation = []
    kind = "q" if queue else "d"
    if offset:
        navigation.append(button("‹ Назад", "list", kind, max(0, offset - PAGE_SIZE)))
    if len(entries) > PAGE_SIZE:
        navigation.append(button("Дальше ›", "list", kind, offset + PAGE_SIZE))
    if navigation:
        rows.append(navigation)
    rows.append([menu_button(not queue), home_button()])

    drafts, queued, _ = store.counts(user_id, chat_id)
    if queue:
        head = f"<b>🕓 Очередь</b> · {queued} · время по Москве"
        text = head + ("\n\nОткройте запись, чтобы перенести или отправить сразу." if entries
                       else "\n\nОчередь пуста. Готовый черновик можно поставить в очередь.")
    else:
        head = f"<b>📝 Черновики</b> · {drafts}"
        text = head + ("\n\n🟢 готов к публикации · 🟡 нужно дополнить" if entries
                       else "\n\nЧерновиков пока нет. Пришлите ссылку или артикул товара.")
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_home(bot: Bot, store: DraftStore, user_id: int, chat_id: int) -> None:
    drafts, queued, next_due = store.counts(user_id, chat_id)
    midnight = datetime.now(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    posts, _, _ = store.history.statistics(CHANNEL_ID, int(midnight.timestamp()))
    text = ui.home_text(drafts, queued, format_time(next_due, short=True) if next_due else "", len(posts))
    await bot.send_message(chat_id, text, reply_markup=main_menu(drafts, queued), link_preview_options=NO_PREVIEW)


def owned_draft(store: DraftStore, key: str, editor: tuple[int, int]) -> Draft | None:
    draft = store.get(key)
    if draft and (draft.owner_id, draft.chat_id) == editor:
        return draft
    return None


async def schedule_view(bot: Bot, store: DraftStore, draft: Draft, editor: tuple[int, int]) -> None:
    tomorrow = datetime.now(MOSCOW).date() + timedelta(days=1)
    example = tomorrow.strftime("%d.%m.%Y") + " 18:30"
    title = "🕓 Перенести публикацию" if draft.state == "queued" else "🕓 Поставить в очередь"
    text = (f"<b>{title}</b>\n\nВыберите время кнопкой или пришлите дату и время по Москве "
            f"в формате <code>{example}</code>.")
    request = Input(draft.id, draft.revision, "schedule")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [action("⏱ Через 30 мин", "soon", draft, 30), action("🕑 Через 2 часа", "soon", draft, 120)],
        [action("📅 В ближайший слот", "slot", draft)],
        [button("✖ Отмена", "cancel", request.nonce)],
    ])
    if await send_view(bot, store, draft, text, markup):
        INPUTS[editor] = request


async def save_schedule(
    bot: Bot, store: DraftStore, draft: Draft, editor: tuple[int, int], timestamp: int
) -> None:
    destination = draft.destination if draft.state == "queued" else CHANNEL_ID
    if destination is None:
        await bot.send_message(draft.chat_id, "Укажите CHANNEL_ID в .env.")
        return
    if not store.schedule(draft, timestamp, destination):
        await bot.send_message(draft.chat_id, "Не удалось запланировать. Откройте черновик заново.")
        return

    INPUTS.pop(editor, None)
    await refresh(bot, store, draft)


async def edit_value(
    message: Message, bot: Bot, store: DraftStore, editor: tuple[int, int], request: Input
) -> None:
    draft = owned_draft(store, request.key, editor)
    if draft is None or request.revision != draft.revision:
        INPUTS.pop(editor, None)
        await message.answer("Запись уже изменилась. Откройте /drafts или /queue.")
        return

    value = (message.text or "").strip()
    if request.field == "screenshot":
        try:
            image = await attached_photo(message, bot)
            if not image:
                raise ParseError(PROMPTS["screenshot"])
            choices = await recognize(image)
        except ParseError as error:
            await message.answer(html.escape(str(error)), reply_markup=cancel_keyboard(request))
            return
        if INPUTS.get(editor) is not request:
            return
        if not choices:
            await message.answer("Не нашёл сумму со знаком рубля. Пришлите крупный фрагмент цены или введите её через «Цена».",
                                 reply_markup=cancel_keyboard(request))
            return
        choice = replace(request, choices=choices, nonce=secrets.token_hex(4))
        INPUTS[editor] = choice
        buttons = [button(fmt_price(amount), "ocr", choice.nonce, i) for i, amount in enumerate(choices)]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows.extend(cancel_keyboard(choice).inline_keyboard)
        await message.answer("Сверьте со скриншотом и нажмите на обычную цену товара.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    if request.field == "schedule":
        try:
            when = parse_time(value)
        except (ValueError, OverflowError, OSError):
            await message.answer("Введите будущую дату и время по Москве в формате ДД.ММ.ГГГГ ЧЧ:ММ.")
            return
        await save_schedule(bot, store, draft, editor, when)
        return

    heading, items = draft.heading, list(draft.items)
    comment, erid = None, None
    if request.field == "prices":
        try:
            items = list(apply_prices(value, draft.items))
        except ValueError as error:
            await message.answer(html.escape(str(error)) + "\n" + PROMPTS["prices"], reply_markup=cancel_keyboard(request))
            return
    elif request.field == "heading":
        if not value:
            await message.answer(PROMPTS["heading"])
            return
        heading = short_text(value, 70)
    elif request.field == "comment":
        if not value:
            await message.answer(PROMPTS["comment"], reply_markup=cancel_keyboard(request))
            return
        comment = "" if value == "-" else short_text(value, 120)
    elif request.field == "erid":
        if value != "-" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            await message.answer(PROMPTS["erid"], reply_markup=cancel_keyboard(request))
            return
        erid = "" if value == "-" else value
    else:
        item = items[request.index]
        if request.field == "quick":
            try:
                changed, used = await supplement_message(item, message, bot, quick=True)
            except ParseError as error:
                await message.answer(html.escape(str(error)), reply_markup=cancel_keyboard(request))
                return
            if not used:
                await message.answer(PROMPTS["quick"], reply_markup=cancel_keyboard(request))
                return
            items[request.index] = changed
        elif request.field == "photo":
            try:
                photo = await attached_photo(message, bot)
            except ParseError as error:
                await message.answer(html.escape(str(error)))
                return
            if not photo:
                await message.answer(PROMPTS["photo"])
                return
            items[request.index] = replace(item, photo=photo, photo_index=None)
        elif request.field == "price":
            price = parse_price(value)
            if price is None and value != "-":
                await message.answer(PROMPTS["price"])
                return
            items[request.index] = replace(
                item, product=manual_price(item.product, price)
            )
        else:
            if not value:
                await message.answer(PROMPTS["title"])
                return
            items[request.index] = replace(item, product=replace(item.product, title=value))

    if INPUTS.get(editor) is not request:
        return
    INPUTS.pop(editor, None)
    if store.update(draft, heading, tuple(items), comment=comment, erid=erid):
        await refresh(bot, store, draft)
    else:
        await message.answer("Запись уже изменилась. Откройте черновик заново.")


async def retry_missing(cb, bot, store, draft, index, slots, *, external=False):
    item = draft.items[index]
    await cb.answer("Проверяю артикул в WBCON…" if external else "Загружаю недостающие поля…")
    try:
        if external:
            async with slots:
                candidate = await wbcon.lookup(item.product)
                photo, position = (None, None)
                if not item.photo and candidate.image_urls:
                    try:
                        photo, position = await asyncio.wait_for(download_jpeg(candidate), 18)
                    except asyncio.TimeoutError:
                        pass
            fresh = DraftItem(candidate, photo, position)
            warning = "WBCON не вернул недостающие данные."
        else:
            fresh, warning = await retrieve_item(item.product.marketplace, item.product.url, slots, need_photo=not item.photo)
    except ParseError as error:
        await bot.send_message(draft.chat_id, html.escape(str(error)))
        return
    candidate = fresh.product
    product = replace(item.product, title=item.product.title or candidate.title)
    if not item.product.price and candidate.price:
        product = replace(product, price=candidate.price, old_price=candidate.old_price, price_note=candidate.price_note,
                          price_source=candidate.price_source, wb_dest=candidate.wb_dest, conditional_price=candidate.conditional_price,
                          conditional_note=candidate.conditional_note, price_unavailable=False)
    photo, position = item.photo, item.photo_index
    if not photo and fresh.photo:
        product = replace(product, image_urls=candidate.image_urls)
        photo, position = fresh.photo, fresh.photo_index
    revised = DraftItem(product, photo, position)
    if revised.missing == item.missing:
        reason = html.escape(warning or "Новых данных пока нет.")
        await bot.send_message(draft.chat_id, reason + " Откройте товар по ссылке в черновике и заполните поле вручную.")
        return
    product.url = candidate.url
    items = list(draft.items)
    items[index] = revised
    if store.update(draft, draft.heading, tuple(items)):
        await refresh(bot, store, draft, index)
    else:
        await bot.send_message(draft.chat_id, "Черновик уже изменился. Откройте его снова в /drafts.")


async def change_item(cb: CallbackQuery, bot: Bot, store: DraftStore, draft: Draft, name: str, index: int) -> None:
    items = list(draft.items)
    item = items[index]
    keep_open = None
    if name == "gallery":
        product = item.product
        await cb.answer("Загружаю фото…")
        if not product.image_urls:
            try:
                current = await asyncio.wait_for(FETCHERS[product.marketplace](product.url), timeout=42)
            except (ParseError, asyncio.TimeoutError):
                await bot.send_message(draft.chat_id, "Витрина пока не отдала фото. Можно прислать своё.")
                return
            product = replace(product, image_urls=current.image_urls[:3])
        count = len(product.image_urls)
        start = 0 if item.photo_index is None else item.photo_index + 1
        steps = count if item.photo_index is None else count - 1
        order = tuple((start + offset) % count for offset in range(steps))
        try:
            photo, position = await asyncio.wait_for(download_jpeg(product, order), timeout=18)
        except asyncio.TimeoutError:
            photo, position = None, None
        if photo is None:
            await bot.send_message(draft.chat_id, "Следующее фото недоступно. Текущее сохранено.")
            return
        items[index] = replace(item, product=product, photo=photo, photo_index=position)
        keep_open = index
    elif name == "shorten":
        title = shorten_name(item.product.title)
        if not title or title == item.product.title:
            await cb.answer("Название уже короткое.")
            return
        items[index] = replace(item, product=replace(item.product, title=title))
        keep_open = index
        await cb.answer("Название сокращено.")
    elif name in ("up", "down"):
        other = index + (-1 if name == "up" else 1)
        if not 0 <= other < len(items):
            await cb.answer("Это крайняя позиция.")
            return
        items[index], items[other] = items[other], items[index]
        await cb.answer("Порядок обновлён.")
    else:
        if len(items) == 1:
            await cb.answer("Карточку можно удалить кнопкой «Убрать».", show_alert=True)
            return
        items.pop(index)
        await cb.answer("Товар убран из подборки.")

    heading = draft.heading if len(items) > 1 else ""
    if store.update(draft, heading, tuple(items)):
        await refresh(bot, store, draft, keep_open)
    else:
        await bot.send_message(draft.chat_id, "Запись уже изменилась. Откройте черновик заново.")


async def send_statistics(bot: Bot, store: DraftStore, chat_id: int, days: int) -> None:
    periods = [button(("● " if period == days else "") + label, "stats", period) for period, label in PERIODS.items()]
    markup = InlineKeyboardMarkup(inline_keyboard=[periods, [home_button()]])
    await bot.send_message(
        chat_id, render_stats(store.history, CHANNEL_ID, days), reply_markup=markup, link_preview_options=NO_PREVIEW
    )


async def send_history(bot: Bot, store: DraftStore, chat_id: int) -> None:
    await bot.send_message(
        chat_id, render_history(store.history, CHANNEL_ID), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_button()]]),
        link_preview_options=NO_PREVIEW,
    )


async def send_help(bot: Bot, chat_id: int) -> None:
    await bot.send_message(chat_id, ui.HELP_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_button()]]),
                           link_preview_options=NO_PREVIEW)


def article_keyboard(sku: str, action_name: str = "art", *context: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        button("🟣 Wildberries", action_name, "wb", sku, *context),
        button("🔵 Ozon", action_name, "ozon", sku, *context),
    ]])


def message_references(message: Message) -> str:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    hidden = [e for e in entities if e.type == "text_link" and e.url]
    if not hidden:
        return text
    # Telegram измеряет смещения в единицах UTF-16. Замена с конца сохраняет смещения.
    raw = text.encode("utf-16-le")
    for entity in sorted(hidden, key=lambda e: e.offset, reverse=True):
        start = entity.offset * 2
        end = start + entity.length * 2
        raw = raw[:start] + (" " + entity.url + " ").encode("utf-16-le") + raw[end:]
    return raw.decode("utf-16-le")


def duplicate_note(store: DraftStore, products: list[Product]) -> str:
    lines = []
    for index, product in enumerate(products, 1):
        previous = store.history.previous(product, CHANNEL_ID)
        if previous is None:
            continue
        title = html.escape(short_text(product.title or previous["title"], 55))
        price = fmt_price(Decimal(previous["price"]))
        if previous["price_note"]:
            price += " · " + html.escape(previous["price_note"])
        line = f"{index}. {title} · было {price}"
        if previous["post_url"]:
            line += f' · <a href="{html.escape(previous["post_url"], quote=True)}">Прошлый пост</a>'
        lines.append(line)
    return "Эти товары уже публиковались\n" + "\n".join(lines) if lines else ""


def wb_by_article(text: str) -> str | None:
    if re.fullmatch(r"[0-9]{1,16}", text) and int(text) > 0:
        return f"https://www.wildberries.ru/catalog/{int(text)}/detail.aspx"
    return None


@router.message(CommandStart())
async def on_start(message: Message, bot: Bot, store: DraftStore) -> None:
    if not message.from_user:
        return
    if not is_admin(message.from_user.id):
        await message.answer(f"Бот закрытый. Ваш ID <code>{message.from_user.id}</code>")
        return
    editor = (message.from_user.id, message.chat.id)
    INPUTS.pop(editor, None)
    await message.answer(ui.WELCOME, reply_markup=navigation_keyboard())
    await show_home(bot, store, *editor)


@router.message(Command("menu"))
async def on_menu(message: Message, bot: Bot, store: DraftStore) -> None:
    if message.from_user and is_admin(message.from_user.id):
        editor = (message.from_user.id, message.chat.id)
        INPUTS.pop(editor, None)
        await show_home(bot, store, *editor)


@router.message(Command("help"))
async def on_help(message: Message, bot: Bot) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_help(bot, message.chat.id)


@router.message(Command("id"))
async def on_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Ваш ID <code>{message.from_user.id}</code>")


@router.message(Command("cancel"))
async def on_cancel(message: Message) -> None:
    if message.from_user and INPUTS.pop((message.from_user.id, message.chat.id), None):
        await message.answer("✖ Ввод отменён.", reply_markup=main_menu())


@router.message(Command("drafts", "queue"))
async def on_list(message: Message, bot: Bot, store: DraftStore) -> None:
    if message.from_user and is_admin(message.from_user.id):
        editor = (message.from_user.id, message.chat.id)
        INPUTS.pop(editor, None)
        command = (message.text or message.caption or "").strip().lower().partition(" ")[0].split("@", 1)[0]
        await send_list(bot, store, *editor, queue=command == "/queue")


@router.message(Command("stats", "history", "views"))
async def on_reports(message: Message, bot: Bot, store: DraftStore) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    INPUTS.pop((message.from_user.id, message.chat.id), None)
    args = (message.text or message.caption or "/").split()
    command = args[0].split("@", 1)[0].lower()
    if command == "/stats":
        period = int(args[1]) if len(args) == 2 and args[1] in ("1", "7", "30") else 7
        await send_statistics(bot, store, message.chat.id, period)
    elif command == "/history":
        await send_history(bot, store, message.chat.id)
    else:
        if len(args) != 3 or not re.fullmatch(r"[0-9]{1,12}", args[2]):
            await message.answer("Используйте /views 42 1200. Первое число — номер поста, второе — просмотры.")
            return
        post: int | str = int(args[1]) if re.fullmatch(r"[0-9]{1,12}", args[1]) else args[1]
        changed = store.history.set_views(CHANNEL_ID, post, int(args[2]))
        await message.answer("Просмотры записаны." if changed else "Пост не найден в истории этого канала.")


async def diagnose(value, slots):
    settings = []
    for market_name in ("wb", "ozon"):
        fallback = os.getenv(f"{market_name.upper()}_PRICE_FALLBACK", "1") == "1"
        key_set = bool(os.getenv(f"BHAPI_{market_name.upper()}_TOKEN", "").strip())
        settings.append(f"{market_name.upper()} · WBCON {'вкл.' if fallback else 'выкл.'} · "
                        f"BHAPI {'подключён' if key_set else 'без ключа'}")
    if not value:
        return ("<b>🩺 Диагностика BorzMarket</b>\n" + "\n".join(settings) + "\n" + net.describe() + "\n\n"
                "Отправьте /diag и ссылку из приложения, либо <code>/diag ozon:1526882900</code> "
                "или <code>/diag wb:866768707</code>.")
    try:
        market, url = market_url(value)
        with capture() as steps:
            results = await retrieve_links([(market, url)], slots, fresh=True)
        if isinstance(results[0], Exception):
            if isinstance(results[0], ParseError):
                raise results[0]
            raise ParseError("Сбой разбора ответа.")
        item, warning = results[0]
    except ParseError as error:
        return html.escape(str(error))
    p = item.product
    lines = [f"<b>🩺 Диагностика · {p.market_name}</b>", settings[0 if market == "wb" else 1], net.describe()]
    lines.extend(html.escape(line) for line in steps)
    price = fmt_price(p.price) if p.price else "нет"
    if p.price_source:
        price += " · " + p.price_source
    if p.price_note:
        price += " · " + p.price_note
    lines += ["", "Название · " + html.escape(short_text(p.title, 90) or "нет"),
              "Цена · " + html.escape(price),
              f"Фото · ссылок {len(p.image_urls)}, " + ("скачано" if item.photo else "файл не получен")]
    if warning:
        lines.append(html.escape(short_text(warning, 400)))
    if item.missing:
        lines.append("В черновике можно нажать «⚡ Заполнить быстро».")
    return "\n".join(lines)


@router.message(Command("diag"))
async def on_diag(message: Message, fetch_slots: asyncio.Semaphore):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and re.fullmatch(r"[0-9]{1,16}", parts[1].strip()) and int(parts[1]):
        await message.answer("Для какой площадки проверить артикул?",
                             reply_markup=article_keyboard(parts[1].strip(), "diagart"))
        return
    status = await message.answer("Проверяю загрузку…" if len(parts) == 2 else await diagnose("", fetch_slots))
    if len(parts) == 2:
        await status.edit_text(await diagnose(parts[1].strip(), fetch_slots), link_preview_options=NO_PREVIEW)


@router.message(Command("wb", "ozon"))
async def on_article(message: Message, bot: Bot, store: DraftStore, fetch_slots: asyncio.Semaphore) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").split(maxsplit=1)
    command = text[0].split("@")[0].lower()
    url = wb_by_article(text[1].strip()) if len(text) == 2 else None
    if url and command == "/ozon":
        url = f"https://www.ozon.ru/product/{int(text[1])}/"
    if url is None:
        await message.answer(f"Пришлите команду и артикул, например {command} 12345678.")
        return
    editor = (message.from_user.id, message.chat.id)
    request = INPUTS.get(editor)
    if request and request.field == "append":
        await append_links(message, bot, store, fetch_slots, editor, request, [url])
        return
    INPUTS.pop(editor, None)
    await collect_links(message, bot, store, fetch_slots, [url])


@router.message(F.text.in_(set(ui.REPLY_ACTIONS)))
async def on_navigation(message: Message, bot: Bot, store: DraftStore):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    editor = (message.from_user.id, message.chat.id)
    INPUTS.pop(editor, None)
    target = ui.REPLY_ACTIONS[message.text]
    if target in ("drafts", "queue"):
        await send_list(bot, store, *editor, queue=target == "queue")
    elif target == "history":
        await send_history(bot, store, editor[1])
    elif target == "stats":
        await send_statistics(bot, store, editor[1], 7)
    elif target == "help":
        await send_help(bot, editor[1])
    else:
        await show_home(bot, store, *editor)


def html_attachment(message):
    doc = message.document
    return bool(doc and ((doc.file_name or "").lower().endswith((".htm", ".html")) or doc.mime_type == "text/html"))


@router.message(F.document, html_attachment)
async def on_saved_page(message: Message, bot: Bot, store: DraftStore, fetch_slots: asyncio.Semaphore):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if not message.document.file_size or message.document.file_size > 8 * 1024 * 1024:
        await message.answer("Для импорта нужен HTML-файл размером до 8 МБ.")
        return
    try:
        async with fetch_slots:
            data = io.BytesIO()
            await bot.download(message.document.file_id, destination=data)
            markup = data.getvalue().decode("utf-8-sig")
            product = await asyncio.to_thread(saved_markup, (message.caption or "").strip(), markup)
            if not (product.title or product.price or product.image_urls):
                raise ParseError("В сохранённой странице нет полей этой карточки.")
            try:
                photo, index = await asyncio.wait_for(download_jpeg(product), 18)
            except asyncio.TimeoutError:
                photo, index = None, None
    except ParseError as error:
        await message.answer(html.escape(str(error)))
        return
    except (UnicodeError, TelegramAPIError, ValueError, RecursionError):
        await message.answer("Не удалось прочитать страницу. Нужен HTML-файл карточки в UTF-8.")
        return
    INPUTS.pop((message.from_user.id, message.chat.id), None)
    item = DraftItem(product, photo, index)
    draft = store.create(message.from_user.id, message.chat.id, (item,))
    await message.answer("Черновик из сохранённой страницы. Сверьте цену на сегодня.")
    await show_preview(bot, store, draft)


@router.message(F.text | F.photo | F.document)
async def on_message(
    message: Message, bot: Bot, store: DraftStore, fetch_slots: asyncio.Semaphore
) -> None:
    if not message.from_user:
        return
    if not is_admin(message.from_user.id):
        if message.text:
            await message.answer(f"Нет доступа. Ваш ID <code>{message.from_user.id}</code>")
        return

    editor = (message.from_user.id, message.chat.id)
    value = (message.text or message.caption or "").strip()
    raw_urls = extract_urls(message_references(message))
    parsed = parse_article(value) if not raw_urls else None
    if request := INPUTS.get(editor):
        if request.field == "append":
            if parsed and not parsed[0]:
                if parsed[1] == str(editor[0]):
                    await message.answer("Это ваш Telegram ID. Пришлите артикул товара.")
                    return
                choice = replace(request, nonce=secrets.token_hex(4))
                INPUTS[editor] = choice
                await message.reply("На какой площадке искать товар для этой подборки?",
                                    reply_markup=article_keyboard(parsed[1], "appendart", choice.nonce))
                return
            await append_links(message, bot, store, fetch_slots, editor, request, raw_urls)
            return
        for url in raw_urls:
            try:
                product_link(url)
            except LinkError:
                continue
            INPUTS.pop(editor, None)
            break
        else:
            await edit_value(message, bot, store, editor, request)
            return
    if not value:
        return

    if not raw_urls and value == str(message.from_user.id):
        await message.answer("Это ваш Telegram ID. Пришлите ссылку на товар или артикул.", reply_markup=main_menu())
        return
    if parsed:
        market, sku = parsed
        if market is None:
            # Артикулы WB и Ozon пересекаются по длине, угадывать площадку нельзя.
            await message.reply(f"🔎 Артикул <code>{sku}</code>\nНа какой площадке искать товар?",
                                 reply_markup=article_keyboard(sku))
            return
        raw_urls = [article_url(market, sku)]
    if not raw_urls:
        await message.answer(
            "Пришлите ссылку на товар Ozon или Wildberries, артикул или несколько ссылок для подборки.",
            reply_markup=main_menu(),
        )
        return
    await collect_links(message, bot, store, fetch_slots, raw_urls)


async def attached_photo(message, bot):
    photos = getattr(message, "photo", None)
    document = getattr(message, "document", None)
    image = photos[-1] if isinstance(photos, (list, tuple)) and photos else None
    mime = getattr(document, "mime_type", "")
    if image is None and isinstance(mime, str) and mime.startswith("image/"):
        image = document
    if image is None:
        return None
    if (image.file_size or 0) > 10 * 1024 * 1024:
        raise ParseError("Изображение больше 10 МБ. Пришлите его как фото в Telegram.")
    try:
        buffer = io.BytesIO()
        await bot.download(image.file_id, destination=buffer)
        return await asyncio.to_thread(convert_to_jpeg, buffer.getvalue())
    except (TelegramAPIError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ParseError("Не удалось прочитать изображение. Пришлите другое фото.") from error


async def supplement_message(item: DraftItem, message: Message, bot: Bot, *, quick=False):
    value = message.text or message.caption or ""
    value = value if isinstance(value, str) else ""
    title, quote = message_fields(item.product, normalize_references(value), quick=quick)
    product = item.product
    if quote and (quick or product.price is None or product.price_source in ("wbcon", "bhapi")):
        product = quote
    if title and (quick or not product.title):
        product = replace(product, title=title)
    photo = await attached_photo(message, bot)
    used = product is not item.product or photo is not None
    return DraftItem(product, photo or item.photo, None if photo else item.photo_index), used


def apply_prices(value, items):
    updated = list(items)
    seen = set()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Пришлите хотя бы одну цену.")
    for number, line in enumerate(lines,1):
        match = re.fullmatch(r"(\d{1,2})\s*[=:]\s*(.+)",line)
        if not match:
            raise ValueError(f"Строка {number}: нужен номер и цена через знак =.")
        index = int(match[1])-1
        amount = parse_price(match[2])
        if not 0 <= index < len(items) or index in seen or amount is None:
            raise ValueError(f"Проверьте номер и цену в строке {number}.")
        seen.add(index)
        p = items[index].product
        updated[index] = replace(items[index], product=manual_price(p, amount))
    return tuple(updated)


async def append_links(message, bot, store, slots, editor, request, raw_urls, *, hints=True):
    draft = owned_draft(store,request.key,editor)
    if not draft or draft.state != "draft" or draft.revision != request.revision:
        INPUTS.pop(editor,None)
        await message.answer("Черновик изменился. Откройте /drafts.")
        return
    if not raw_urls:
        await message.answer(PROMPTS["append"],reply_markup=cancel_keyboard(request))
        return
    try:
        links = [product_link(url) for url in raw_urls]
    except LinkError as error:
        await message.answer(html.escape(str(error)), reply_markup=cancel_keyboard(request))
        return
    existing = {product_key(item.product,variant=True) for item in draft.items}
    links = [(m,str(u)) for m,u in links if product_key(Product(m,"",str(u)),variant=True) not in existing]
    if not links or len(links) > MAX_ITEMS-len(draft.items):
        text = "Эти товары уже есть в подборке." if not links else f"Можно добавить ещё {MAX_ITEMS-len(draft.items)}."
        await message.answer(text, reply_markup=cancel_keyboard(request))
        return
    status = await message.answer("Добавляю товары в подборку…")
    responses = await retrieve_links(links, slots)
    if INPUTS.get(editor) is not request:
        await status.edit_text("Добавление отменено.")
        return
    new_items = list(draft.items)
    notices, events = [], []
    for pos, ((market,_),response) in enumerate(zip(links,responses),1):
        if isinstance(response,LinkError):
            notices.append(f"Ссылка {pos}: " + html.escape(str(response)))
            events.append((market,fetch_result(None,str(response))))
            continue
        if isinstance(response,BaseException):
            raise response
        item,warning = response
        if len(raw_urls) == 1 and hints:
            try:
                item, _ = await supplement_message(item, message, bot)
            except ParseError as error:
                notices.append(html.escape(str(error)))
        events.append((market,fetch_result(item,warning)))
        key = product_key(item.product,variant=True)
        if key in existing:
            notices.append(f"Ссылка {pos}: товар уже в подборке.")
            continue
        existing.add(key)
        new_items.append(item)
        if warning:
            notices.append(f"Товар {len(new_items)}: " + html.escape(warning))
    store.history.record_fetches(int(time.time()),events)
    if len(new_items) == len(draft.items):
        await status.edit_text("\n".join(notices) or "Новых товаров нет.", reply_markup=cancel_keyboard(request))
        return
    INPUTS.pop(editor,None)
    if not store.update(draft,draft.heading or "Подборка BorzMarket",tuple(new_items)):
        await status.edit_text("Черновик изменился. Откройте /drafts.")
        return
    if notices:
        await status.edit_text("\n".join(notices))
    else:
        with suppress(TelegramAPIError):
            await status.delete()
    await refresh(bot,store,draft)


async def collect_links(
    message: Message, bot: Bot, store: DraftStore, fetch_slots: asyncio.Semaphore, raw_urls: list[str],
    *, user_id: int | None = None, hints: bool = True,
) -> None:
    owner = user_id or message.from_user.id
    try:
        links = [product_link(url) for url in raw_urls]
    except LinkError as error:
        await message.answer(html.escape(str(error)))
        return
    # Один и тот же товар дважды в сообщении занимал бы два места в подборке.
    unique = {}
    for market, url in links:
        unique.setdefault(product_key(Product(market, "", str(url)), variant=True), (market, url))
    links = list(unique.values())
    if len(links) > MAX_ITEMS:
        await message.answer(f"В одну подборку можно добавить до {MAX_ITEMS} товаров. Разделите ссылки на два сообщения.")
        return

    early = duplicate_note(store, [Product(market, "", str(url)) for market, url in links])
    label = "⏳ Собираю карточку…" if len(links) == 1 else f"⏳ Собираю подборку из {len(links)} товаров…"
    status = await message.answer(
        label + ("\n\n" + early if early else ""), link_preview_options=NO_PREVIEW
    )
    responses = await retrieve_links(links, fetch_slots)
    items, warnings, events, invalid = [], [], [], []
    for index, (response, (market, _)) in enumerate(zip(responses, links), 1):
        if isinstance(response, LinkError):
            events.append((market, fetch_result(None, str(response))))
            invalid.append(f"Ссылка №{index}. {html.escape(str(response))}")
            continue
        if isinstance(response, BaseException):
            raise response
        item, warning = response
        items.append(item)
        events.append((market, fetch_result(item, warning)))
        if warning:
            warnings.append(f"{index}. {html.escape(warning)}")

    store.history.record_fetches(int(time.time()), events)
    if not items:
        await status.edit_text("\n".join(invalid))
        return

    supplied = False
    if len(links) == 1 and hints:
        # A share sheet or a forwarded photo can complete a single draft in one message.
        try:
            items[0], supplied = await supplement_message(items[0], message, bot)
        except ParseError as error:
            supplied = True
            invalid.append(html.escape(str(error)))
    draft = store.create(owner, message.chat.id, tuple(items))
    duplicate = duplicate_note(store, [item.product for item in items]) or early
    external_price = any(item.product.price_source in ("wbcon", "bhapi") for item in items)
    if (AUTO_POST and CHANNEL_ID is not None and len(links) == 1 and draft.ready
            and not duplicate and not supplied and not external_price and not items[0].product.used_fallback):
        result, markup = await publish(bot, store, draft, CHANNEL_ID, avoid_repeat=True)
        await status.edit_text(result, reply_markup=markup)
        pending = store.get(draft.id)
        if pending and pending.state == "draft":
            await show_preview(bot, store, pending)
        return

    try:
        await show_preview(bot, store, draft)
    except TelegramAPIError:
        await status.edit_text("Черновик сохранён. Откройте /drafts, чтобы попробовать показать его ещё раз.")
        return
    notices = [duplicate] if duplicate else []
    if invalid:
        notices.append("Пропущены ссылки, которые не удалось открыть.\n" + "\n".join(invalid))
    if supplied:
        notices.append("Использованы данные из вашего сообщения. Проверьте черновик.")
    if external_price:
        notices.append("Цена из резервного источника добавлена в черновик. Сверьте её перед публикацией.")
    if warnings and not draft.ready:
        notices.append("Данные можно дополнить в черновике.\n" + "\n".join(warnings))
    if notices:
        await status.edit_text("\n\n".join(notices), link_preview_options=NO_PREVIEW)
    else:
        with suppress(TelegramAPIError):
            await status.delete()


@router.callback_query(F.data.startswith("bm:"))
async def on_callback(cb: CallbackQuery, bot: Bot, store: DraftStore, fetch_slots: asyncio.Semaphore) -> None:
    if cb.message is None:
        await cb.answer("Сообщение недоступно.", show_alert=True)
        return
    _, name, *args = (cb.data or "").split(":")
    if name == "react" and len(args) == 1:
        async with REACTION_LOCK:
            reaction = store.history.react(
                args[0], cb.message.chat.id, cb.message.message_id, cb.from_user.id
            )
            if reaction is None:
                await cb.answer("Пост ещё сохраняется или недоступен.", show_alert=True)
                return
            total, added = reaction
            with suppress(TelegramAPIError):
                await cb.answer("Реакция добавлена." if added else "Реакция снята.")
            with suppress(TelegramAPIError):
                await bot.edit_message_reply_markup(
                    chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                    reply_markup=refresh_reaction(cb.message.reply_markup, args[0], total),
                )
        return
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    editor = (cb.from_user.id, cb.message.chat.id)
    if name == "home":
        INPUTS.pop(editor, None)
        await cb.answer()
        await show_home(bot, store, *editor)
        return
    if name == "help":
        await cb.answer()
        await send_help(bot, editor[1])
        return
    if name == "diagmenu":
        await cb.answer()
        await bot.send_message(editor[1], await diagnose("", fetch_slots),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_button()]]))
        return
    if name in {"art", "appendart", "diagart"}:
        is_append = name == "appendart"
        if (len(args) != (3 if is_append else 2) or args[0] not in ("wb", "ozon")
                or not re.fullmatch(r"[0-9]{1,16}", args[1]) or not int(args[1])):
            await cb.answer("Некорректный артикул.", show_alert=True)
            return
        url = article_url(args[0], args[1])
        if name == "diagart":
            await cb.answer()
            await cb.message.edit_text(await diagnose(url, fetch_slots), link_preview_options=NO_PREVIEW)
            return
        request = INPUTS.get(editor)
        if is_append:
            if not request or request.field != "append" or request.nonce != args[2]:
                await cb.answer("Выбор устарел. Откройте подборку заново.", show_alert=True)
                return
            request = replace(request, nonce=secrets.token_hex(4))
            INPUTS[editor] = request
        origin = getattr(cb.message, "reply_to_message", None)
        if origin and origin.from_user and origin.from_user.id == cb.from_user.id:
            source = origin
        else:
            source = cb.message
        await cb.answer()
        with suppress(TelegramAPIError):
            await cb.message.delete()
        if is_append:
            if INPUTS.get(editor) is request:
                await append_links(source, bot, store, fetch_slots, editor, request, [url], hints=source is origin)
        else:
            await collect_links(source, bot, store, fetch_slots, [url], user_id=cb.from_user.id,
                                hints=source is origin)
        return
    if name == "ocr":
        request = INPUTS.get(editor)
        if (not request or request.field != "screenshot" or len(args) != 2 or args[0] != request.nonce
                or not re.fullmatch(r"[0-7]", args[1]) or int(args[1]) >= len(request.choices)):
            await cb.answer("Выбор уже закрыт.")
            return
        draft = owned_draft(store, request.key, editor)
        if not draft or draft.revision != request.revision or draft.state != "draft":
            INPUTS.pop(editor, None)
            await cb.answer("Черновик изменился. Откройте /drafts.")
            return
        items = list(draft.items)
        item = items[request.index]
        price = manual_price(item.product, request.choices[int(args[1])])
        items[request.index] = replace(item, product=replace(price, price_source="screenshot"))
        saved = store.update(draft, draft.heading, tuple(items))
        INPUTS.pop(editor, None)
        await cb.answer("Цена сохранена." if saved else "Черновик изменился.")
        if saved:
            await clear_view(bot, cb.message.chat.id, cb.message.message_id)
            await refresh(bot, store, draft)
        return
    if name == "cancel":
        request = INPUTS.get(editor)
        if request is None or args != [request.nonce]:
            await cb.answer("Этот ввод уже завершён.")
            return
        INPUTS.pop(editor, None)
        await cb.answer("Ввод отменён.")
        await clear_view(bot, cb.message.chat.id, cb.message.message_id)
        pending = owned_draft(store, request.key, editor)
        if pending is not None:
            await show_preview(bot, store, pending)
        return
    if name == "history":
        INPUTS.pop(editor, None)
        await cb.answer()
        await send_history(bot, store, editor[1])
        return
    if name == "stats" and len(args) == 1 and args[0] in ("1", "7", "30"):
        INPUTS.pop(editor, None)
        await cb.answer()
        await send_statistics(bot, store, editor[1], int(args[0]))
        return
    if name == "list" and len(args) == 2 and args[0] in ("d", "q") and re.fullmatch(r"[0-9]{1,12}", args[1]):
        INPUTS.pop(editor, None)
        await cb.answer()
        await send_list(bot, store, *editor, queue=args[0] == "q", offset=int(args[1]))
        return
    draft = owned_draft(store, args[0], editor) if args else None
    if draft is None:
        await cb.answer("Запись уже обработана или удалена.", show_alert=True)
        return
    if name == "open":
        INPUTS.pop(editor, None)
        await cb.answer()
        await show_preview(bot, store, draft)
        return
    if len(args) < 2 or args[1] != str(draft.revision) or draft.view_id != cb.message.message_id:
        await cb.answer("Откройте актуальную запись в /drafts или /queue.", show_alert=True)
        return

    extra = args[2:]
    INPUTS.pop(editor, None)
    if name == "design" and draft.state == "draft":
        await cb.answer()
        await show_design(bot, store, draft)
        return
    if name == "layout" and draft.state == "draft" and len(draft.items) in LAYOUT_NAMES:
        mode = "alternate" if draft.layout == "auto" else "auto"
        if store.update(draft,draft.heading,draft.items,layout=mode):
            await cb.answer("Макет изменён.")
            await refresh(bot,store,draft)
        else:
            await cb.answer("Черновик изменился. Откройте его снова.")
        return
    if name == "diag" and len(extra) == 1 and extra[0].isdigit() and int(extra[0]) < len(draft.items):
        await cb.answer("Проверяю загрузку…")
        report = await diagnose(draft.items[int(extra[0])].product.url, fetch_slots)
        await bot.send_message(draft.chat_id, report, link_preview_options=NO_PREVIEW)
        return
    if (name in ("retry","external") and draft.state == "draft" and len(extra) == 1
            and re.fullmatch(r"[0-5]", extra[0]) and int(extra[0]) < len(draft.items)):
        await retry_missing(cb, bot, store, draft, int(extra[0]), fetch_slots, external=name=="external")
        return
    if name == "tags" and draft.state == "draft":
        await cb.answer()
        await show_tags(bot, store, draft)
        return
    if name == "tag" and draft.state == "draft" and len(extra) == 1 and extra[0] in ("0", "1", "2", "3"):
        changed = set(draft.tags) ^ {RUBRICS[int(extra[0])]}
        tags = tuple(tag for tag in RUBRICS if tag in changed)
        await cb.answer()
        if store.update(draft, draft.heading, draft.items, tags=tags):
            await clear_view(bot, draft.chat_id, draft.view_id)
            updated = store.get(draft.id)
            if updated:
                await show_tags(bot, store, updated)
        return

    if (
        name in ("gallery", "shorten", "up", "down", "remove") and draft.state == "draft"
        and len(extra) == 1 and re.fullmatch(r"[0-5]", extra[0]) and int(extra[0]) < len(draft.items)
    ):
        await change_item(cb, bot, store, draft, name, int(extra[0]))
        return

    if name == "more" and draft.state == "draft" and len(extra) == 1 and extra[0].isdigit() and int(extra[0]) < len(draft.items):
        await cb.answer()
        await show_item(bot, store, draft, int(extra[0]), more=True)
        return

    if name == "item" and draft.state == "draft" and len(extra) == 1 and extra[0].isdigit():
        index = int(extra[0])
        if index >= len(draft.items):
            await cb.answer("Товар не найден.", show_alert=True)
            return
        await cb.answer()
        await show_item(bot, store, draft, index)
        return

    if (
        name == "edit" and draft.state == "draft" and len(extra) == 2
        and extra[0] in PROMPTS and extra[1].isdigit() and int(extra[1]) < len(draft.items)
    ):
        field, index = extra[0], int(extra[1])
        request = Input(draft.id, draft.revision, field, index)
        INPUTS[editor] = request
        await cb.answer()
        prompt = PROMPTS[field]
        if field == "append":
            prompt += f" Свободных мест: {MAX_ITEMS-len(draft.items)}."
        elif field == "prices":
            prompt += "\n\n" + "\n".join(
                f"{i}. {html.escape(short_text(it.product.title or it.product.market_name,34))} · "
                + (fmt_price(it.product.price) if it.product.price else "цена не указана")
                for i,it in enumerate(draft.items,1))
        await bot.send_message(draft.chat_id, prompt, reply_markup=cancel_keyboard(request))
        return

    if name in ("time", "soon", "slot") and draft.state in ("draft", "queued") and draft.ready:
        destination = draft.destination if draft.state == "queued" else CHANNEL_ID
        if destination is None:
            await cb.answer("Укажите CHANNEL_ID в .env.", show_alert=True)
            return
        if name == "slot":
            when = store.schedule_slot(draft, destination, future_slots(int(time.time()), queue_slots()))
            if when is None:
                await cb.answer("Запись изменилась. Откройте её заново.", show_alert=True)
            else:
                await cb.answer(f"Запланировано на {format_time(when)}.")
                await refresh(bot, store, draft)
            return
        await cb.answer()
        if name == "time":
            await schedule_view(bot, store, draft, editor)
        elif extra in (["30"], ["120"]):
            await save_schedule(bot, store, draft, editor, int(time.time()) + int(extra[0]) * 60)
        return

    if name == "unschedule" and store.unschedule(draft):
        await cb.answer("Возвращено в черновики.")
        await refresh(bot, store, draft)
        return

    if name == "drop" and store.drop(draft):
        await cb.answer("Черновик убран.")
        with suppress(TelegramAPIError):
            await bot.delete_message(chat_id=draft.chat_id, message_id=cb.message.message_id)
        return

    if name == "post" and draft.state in ("draft", "queued") and draft.ready:
        destination = draft.destination if draft.state == "queued" else CHANNEL_ID
        if destination is None:
            await cb.answer("Укажите CHANNEL_ID в .env.", show_alert=True)
            return
        with suppress(TelegramAPIError):
            await cb.answer("Отправляю…")
        text, markup = await publish(bot, store, draft, destination)
        await clear_view(bot, draft.chat_id, draft.view_id)
        await bot.send_message(draft.chat_id, text, reply_markup=markup)
        return

    await cb.answer("Это действие сейчас недоступно.", show_alert=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Задайте BOT_TOKEN в .env")
    if not ADMIN_IDS:
        raise SystemExit("Задайте ADMIN_IDS в .env. Узнать свой ID можно командой /id.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    store = DraftStore(DB_PATH)
    dp = Dispatcher()
    dp.include_router(router)
    workers: list[asyncio.Task[None]] = []
    click_runner = None
    try:
        await bot.me()
        queue_slots()
        monitor = os.getenv("PRICE_MONITOR", "1") == "1"
        if monitor:
            price_threshold()
        click_runner = await start_clicks(store)
        slots = asyncio.Semaphore(2)
        workers.append(asyncio.create_task(run_queue(bot, store)))
        if monitor:
            workers.append(asyncio.create_task(
                run_price_watch(bot, store, ADMIN_IDS, slots, partial(retrieve_item, allow_paid=False), show_preview)
            ))
        await dp.start_polling(bot, close_bot_session=False, store=store, fetch_slots=slots)
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if click_runner is not None:
            await click_runner.cleanup()
        await bot.session.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
