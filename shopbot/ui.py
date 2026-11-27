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
    "<b>🛍 BorzMarket v9.1</b> · редактор канала\n"
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
    "• «↻ Загрузить недостающее» повторяет запрос.\n"
    "• «⚡ Быстрый ввод» принимает строку <code>Термос 500 мл — 1 299 ₽</code>.\n"
    "• «🩺 Диагностика» показывает, на каком шаге остановилась загрузка.\n\n"
    "<b>Команды</b>\n"
    "/drafts · /queue · /history · /stats · /diag · /cancel"
)


def status_notes(draft_items, *, ready: bool, many: bool) -> str:
    """Строка состояния под превью: что осталось заполнить или что всё готово."""
    missing = []
    for index, item in enumerate(draft_items, 1):
        if item.missing:
            prefix = f"{index}. " if many else ""
            missing.append(prefix + ", ".join(item.missing))
    if missing:
        return "\n\n✏️ <b>Осталось заполнить</b>\n" + "\n".join(html.escape(line) for line in missing)
    if ready:
        return "\n\n✅ <b>Готово к публикации</b>" + (" · нажмите номер, чтобы изменить товар" if many else "")
    return ""
