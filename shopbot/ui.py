"""Оформление панели администратора: тексты и подписи. Логики Telegram здесь нет."""
from __future__ import annotations

import html
from decimal import Decimal

from card import fmt_price, short_text

MARKET_ICON = {"wb": "🟣", "ozon": "🔵"}
MARKET_TITLE = {"wb": "Wildberries", "ozon": "Ozon"}

REPLY_ROWS = (
    ("📝 Черновики", "🕓 Очередь"),
    ("📊 Статистика", "🏠 Меню"),
)
# Подписи прежних клавиатур, которые могли остаться в чатах после обновления.
LEGACY_LABELS = {"📚 История": "history", "Помощь": "help", "Черновики": "drafts", "Очередь": "queue"}
REPLY_ACTIONS = {
    "📝 Черновики": "drafts", "🕓 Очередь": "queue", "📊 Статистика": "stats", "🏠 Меню": "home",
    **LEGACY_LABELS,
}


def market_icons(markets: str) -> str:
    return "".join(MARKET_ICON.get(part, "") for part in markets.split("+") if part)


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def list_label(entry, *, queue: bool, when: str = "") -> str:
    """Подпись строки в списке черновиков или очереди."""
    icons = market_icons(entry.markets)
    if entry.count > 1:
        body = f"{icons} {short_text(entry.title, 30)} · {entry.count} {plural(entry.count, 'товар', 'товара', 'товаров')}"
    else:
        price = f" · {fmt_price(Decimal(entry.price))}" if entry.price else ""
        body = f"{icons} {short_text(entry.title, 34)}{price}"
    if entry.state == "sending":
        return "📤 " + body
    if entry.state == "uncertain":
        return "⚠️ " + body
    if queue:
        return f"🕓 {when} · {body}" if when else "🕓 " + body
    return ("🟢 " if entry.ready else "🟡 ") + body


def home_text(drafts: int, queued: int, next_due: str, today: int) -> str:
    lines = [
        "<b>🛍 BorzMarket</b> · панель канала",
        "",
        f"📝 Черновики — <b>{drafts}</b>",
        f"🕓 В очереди — <b>{queued}</b>" + (f" · ближайший {next_due}" if next_due else ""),
        f"✅ Опубликовано сегодня — <b>{today}</b>",
        "",
        "Пришлите ссылку или артикул, и я соберу карточку.",
        "Две–шесть ссылок одним сообщением дадут коллаж-подборку.",
    ]
    return "\n".join(lines)


WELCOME = (
    "<b>🛍 BorzMarket v10.0</b> · редактор канала\n"
    "Внизу — быстрые кнопки. Полная панель открывается кнопкой «🏠 Меню»."
)

HELP_TEXT = (
    "<b>❓ Как пользоваться</b>\n\n"
    "<b>Карточка</b>\n"
    "Пришлите ссылку Ozon или Wildberries. Ссылка может быть без https://.\n"
    "Если прислать только артикул, бот спросит, на какой он площадке. "
    "Заранее можно написать <code>wb 866768707</code> или <code>ozon 1526882900</code>.\n\n"
    "<b>Подборка</b>\n"
    "От 2 до 6 ссылок одним сообщением. Номер под фото совпадает с кнопкой под постом.\n\n"
    "<b>Если данные не загрузились</b>\n"
    "Под черновиком написано, чего не хватает, почему и что сделать. Если не хватает одного поля, "
    "просто пришлите его следующим сообщением: цену числом, название текстом или фото.\n"
    "• «↻ Загрузить недостающее» повторяет запрос, если источник не на паузе.\n"
    "• «⚡ Быстрый ввод» принимает строку <code>Термос 500 мл — 1 299 ₽</code>.\n"
    "• Фото с подписью <code>ozon:1526882900</code> и <code>Термос — 1 299 ₽</code> сразу даёт черновик.\n"
    "• «🩺 Диагностика» показывает каждый источник и строку для пересылки без токенов.\n\n"
    "<b>Команды</b>\n"
    "/drafts · /queue · /history · /stats · /diag · /cancel"
)


FIELD_KEYS = {"название": "title", "цена": "price", "фото": "photo"}


def status_notes(draft_items, *, ready: bool, many: bool) -> str:
    """Строка состояния под превью: что осталось заполнить, почему и что сделать сейчас."""
    from report import next_action
    missing = []
    for index, item in enumerate(draft_items, 1):
        if not item.missing:
            continue
        gaps = item.product.gaps or {}
        if many:
            missing.append(f"{index}. " + ", ".join(item.missing))
            continue
        for label in item.missing:
            reason = gaps.get(FIELD_KEYS[label], "")
            missing.append(label + (f" — {short_text(reason, 110)}" if reason else ""))
    if missing:
        text = "\n\n✏️ <b>Осталось заполнить</b>\n" + "\n".join("• " + html.escape(line) for line in missing)
        if many:
            text += "\n👉 Нажмите «↻ Дозагрузить пустые» или номер с ⚠️, чтобы дополнить вручную."
        else:
            item = draft_items[0]
            if item.product.reference_note:
                text += "\nℹ️ " + html.escape(short_text(item.product.reference_note, 160))
            text += "\n👉 " + html.escape(next_action(item.product, item.photo is not None, item.product.trace))
        return text
    if ready:
        return "\n\n✅ <b>Готово к публикации</b>" + (" · нажмите номер, чтобы изменить товар" if many else "")
    return ""


def alternatives_note(product) -> str:
    """Противоречивые значения показываются как варианты, а не теряются молча."""
    lines = []
    alt = product.alternatives or {}
    if "price" in alt:
        lines.append(f"Цена из {alt['price'].get('label', 'другого источника')}: {fmt_price(Decimal(alt['price']['value']))}")
    if "title" in alt:
        lines.append(f"Название из {alt['title'].get('label', 'другого источника')}: «{short_text(alt['title']['value'], 60)}»")
    return "\n".join(html.escape(line) for line in lines)


def slot_label(timestamp: int, now: int | None = None) -> str:
    """«Сегодня 16:00», «Завтра 10:00» или «28.11 10:00» по Москве."""
    import time
    from datetime import datetime, timedelta, timezone
    zone = timezone(timedelta(hours=3))
    moment = datetime.fromtimestamp(timestamp, zone)
    today = datetime.fromtimestamp(now if now is not None else time.time(), zone).date()
    if moment.date() == today:
        return "Сегодня " + moment.strftime("%H:%M")
    if moment.date() == today + timedelta(days=1):
        return "Завтра " + moment.strftime("%H:%M")
    return moment.strftime("%d.%m %H:%M")
