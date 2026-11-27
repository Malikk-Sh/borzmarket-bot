"""Понятный отчёт об импорте: что получено, почему чего-то нет и что сделать сейчас.

Один и тот же отчёт показывают /diag в Telegram и `prices.py --diag` в терминале.
Обезличенная выгрузка не содержит токенов, ключей, заголовков запросов, ID чатов и
содержимого базы: только версию, платформу, переключатели и исходы обращений.
"""
from __future__ import annotations

import html
import os
import platform
import sys
import time
from datetime import datetime, timedelta, timezone

from card import fmt_price, short_text
from diagnostics import Event
import state

VERSION = "10.0"
MOSCOW = timezone(timedelta(hours=3))

SOURCE_NAMES = {
    "ozon.short": "Короткая ссылка Ozon",
    "ozon.page": "Страница Ozon",
    "wb.detail": "Цены WB",
    "wb.basket": "Каталог фото WB",
    "wb.history": "История цен WB",
    "wbcon.wb": "WBCON · WB",
    "wbcon.ozon": "WBCON · Ozon",
    "bhapi.wb": "BHAPI · WB",
    "bhapi.ozon": "BHAPI · Ozon",
    "photo": "Скачивание фото",
    "memory": "Память бота",
    "tg.preview": "Превью Telegram",
    "message": "Ваше сообщение",
}
OUTCOMES = {
    "ok": "получено", "empty": "нет данных этого товара", "blocked": "доступ закрыт", "limit": "лимит запросов",
    "timeout": "тайм-аут", "error": "ошибка", "skipped": "пропущено", "cached": "из кэша",
}
TITLE_SOURCES = {
    "ozon.page": "страница Ozon", "ozon.slug": "адрес ссылки Ozon (черновое)", "wb.detail": "витрина WB",
    "wb.basket": "каталог WB", "wbcon": "WBCON", "bhapi": "BHAPI", "message": "ваше сообщение",
    "manual": "введено вручную", "memory": "память бота", "tg.preview": "превью Telegram", "saved_html": "сохранённая страница",
}
PRICE_SOURCES = {
    "ozon.webPrice": "витрина Ozon", "ozon.jsonld": "разметка карточки Ozon", "ozon.opengraph": "разметка Ozon",
    "wb.detail": "витрина WB", "wb.jsonld": "разметка WB", "wbcon": "WBCON", "bhapi": "BHAPI",
    "message": "ваше сообщение", "manual": "введено вручную", "screenshot": "скриншот", "memory": "память бота",
}


def moscow(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, MOSCOW).strftime("%H:%M")


def describe_event(event: Event) -> str:
    name = SOURCE_NAMES.get(event.source, event.source)
    parts = [name]
    if event.http:
        parts.append(f"HTTP {event.http}")
    parts.append(OUTCOMES.get(event.outcome, event.outcome))
    if event.ms is not None:
        parts.append(f"{event.ms / 1000:.1f} с".replace(".", ","))
    if event.attempts and event.attempts > 1:
        parts.append(f"попыток {event.attempts}")
    if event.retry_at:
        parts.append(f"повтор после {moscow(event.retry_at)} МСК")
    if event.detail and event.outcome not in ("ok",):
        parts.append(short_text(event.detail, 70))
    elif event.detail and event.source in ("wb.detail", "wbcon.wb", "bhapi.wb", "bhapi.ozon"):
        parts.append(short_text(event.detail, 40))
    return " · ".join(parts)


def _main_failure(events: list[Event], prefix: tuple[str, ...]) -> Event | None:
    for event in events:
        if event.source.startswith(prefix) and event.outcome in ("blocked", "limit", "timeout", "error", "empty", "skipped"):
            return event
    return None


def _short(event: Event) -> str:
    name = SOURCE_NAMES.get(event.source, event.source)
    text = f"{name}: {OUTCOMES.get(event.outcome, event.outcome)}"
    if event.http and event.outcome != "skipped":
        text += f" (HTTP {event.http})"
    if event.retry_at and event.retry_at > time.time():
        text += f", повтор после {moscow(event.retry_at)}"
    return text


def field_gaps(product, has_photo: bool, events: list[Event]) -> dict:
    """Короткая причина для каждого незаполненного поля."""
    market = product.marketplace
    main = ("ozon.",) if market == "ozon" else ("wb.detail",)
    fallbacks = ("wbcon.", "bhapi.")
    gaps = {}
    reasons = [e for e in (_main_failure(events, main), _main_failure(events, fallbacks)) if e]
    base = "; ".join(_short(e) for e in reasons)
    if not product.title.strip():
        gaps["title"] = base or "источник не вернул название"
    elif product.provisional_title:
        gaps["title"] = "черновое название из адреса ссылки — проверьте"
    if product.price_unavailable:
        gaps["price"] = f"{product.market_name}: товара нет в наличии"
    elif product.price is None:
        gaps["price"] = base or "источник не указал цену"
    if not has_photo:
        photo = [e for e in events if e.source == "photo" and e.outcome != "ok"]
        if not product.image_urls:
            gaps["photo"] = base or "у источника нет ссылок на фото"
        elif photo:
            gaps["photo"] = "фото не скачалось" + (f" (HTTP {photo[-1].http})" if photo[-1].http else "")
        else:
            gaps["photo"] = "фото не скачалось"
    return gaps


def retry_moment(events: list[Event]) -> float | None:
    moments = [e.retry_at for e in events if e.retry_at and e.retry_at > time.time()]
    return max(moments) if moments else None


def next_action(product, has_photo: bool, events: list[Event]) -> str:
    """Одно следующее действие для администратора."""
    missing = []
    if not product.title.strip() or product.provisional_title:
        missing.append("title")
    if product.price is None and not product.price_unavailable:
        missing.append("price")
    if not has_photo:
        missing.append("photo")
    if product.price_unavailable:
        return "Товара нет в наличии. Выберите другой вариант или уберите товар из поста."
    if not missing:
        return "Всё готово. Можно публиковать или ставить в очередь."
    retry = retry_moment(events)
    when = f" или повторите после {moscow(retry)} МСК" if retry else ""
    if missing == ["title"] and product.provisional_title:
        return "Проверьте черновое название: нажмите «✅ Название верно» или пришлите своё."
    if missing == ["price"]:
        return "Пришлите цену из приложения одним сообщением, например 1 299" + when + "."
    if missing == ["photo"]:
        return "Пришлите фото товара одним сообщением" + (" или нажмите «🎞 Галерея»" if product.image_urls else "") + "."
    if missing == ["title"]:
        return "Пришлите название товара одним сообщением."
    if "photo" in missing and len(missing) >= 2:
        return ("Пришлите фото товара с подписью «Название — цена», например «Термос — 1 299 ₽»" + when + ".")
    return "Пришлите «Название — цена» одним сообщением, например «Термос — 1 299 ₽»" + when + "."


def mark(value: bool) -> str:
    return "✅" if value else "❌"


def summary(product, has_photo: bool, events: list[Event], *, sku: str = "") -> list[str]:
    """Первые строки отчёта: итог, артикул, поля и одно следующее действие."""
    title_state = mark(bool(product.title.strip()) and not product.provisional_title)
    if product.provisional_title:
        title_state = "⚠️"
    price = product.price
    price_text = fmt_price(price) if price is not None else ("нет в наличии" if product.price_unavailable else "нет")
    if price is not None:
        source = PRICE_SOURCES.get(product.price_source, product.price_source or "источник не указан")
        price_text += f" · {source}"
        if product.price_note:
            price_text += f" · {product.price_note}"
        if product.price_at:
            price_text += f" · {state.age_text(time.time() - product.price_at)}"
    title_source = TITLE_SOURCES.get(product.title_source, product.title_source)
    ready = bool(product.title.strip()) and not product.provisional_title and price is not None and has_photo
    lines = [
        ("✅ Карточка готова" if ready else "🟡 Карточка неполная") + f" · {product.market_name}"
        + (f" · артикул {sku}" if sku else ""),
        f"{title_state} Название · " + (html.escape(short_text(product.title, 80)) if product.title.strip() else "нет")
        + (f" · {title_source}" if product.title.strip() and title_source else ""),
        f"{mark(price is not None)} Цена · {html.escape(price_text)}",
        f"{mark(has_photo)} Фото · " + ("скачано" if has_photo else f"ссылок {len(product.image_urls)}, файл не получен"),
    ]
    if product.conditional_price and product.conditional_price != price:
        lines.append(f"   {html.escape(product.conditional_note)} · {fmt_price(product.conditional_price)}")
    if product.reference_note:
        lines.append("ℹ️ " + html.escape(product.reference_note))
    lines.append("👉 " + html.escape(next_action(product, has_photo, events)))
    return lines


def settings_flags() -> dict:
    import net
    curl = "нет" if net.curl_requests is None else ("сбой" if net._curl_broken else (
        "выкл" if os.getenv("HTTP_IMPERSONATE", "1") == "0" else net.CURL_VERSION or "да"))
    return {
        "wbcon_wb": os.getenv("WB_PRICE_FALLBACK", "1") == "1",
        "wbcon_ozon": os.getenv("OZON_PRICE_FALLBACK", "1") == "1",
        "bhapi_wb": bool(os.getenv("BHAPI_WB_TOKEN", "").strip()),
        "bhapi_ozon": bool(os.getenv("BHAPI_OZON_TOKEN", "").strip()),
        "curl_cffi": curl,
        "wb_dest_default": (os.getenv("WB_DEST") or "-1257786") == "-1257786",
        "tg_preview": bool(os.getenv("TG_API_ID", "").strip() and os.getenv("TG_API_HASH", "").strip()),
    }


def export(results: list[tuple[str, object, bool, list[Event]]]) -> str:
    """Обезличенная выгрузка для пересылки разработчику. Без токенов, ключей и данных базы.

    results — (ввод, product, фото скачано, события) для каждого товара.
    """
    flags = settings_flags()
    head = (f"BM-DIAG v{VERSION} · py{sys.version_info.major}.{sys.version_info.minor} · "
            f"{platform.machine() or '?'} · {platform.system() or '?'} · "
            f"curl_cffi={flags['curl_cffi']} · WBCON wb={int(flags['wbcon_wb'])} oz={int(flags['wbcon_ozon'])} · "
            f"BHAPI wb={int(flags['bhapi_wb'])} oz={int(flags['bhapi_ozon'])} · dest_default={int(flags['wb_dest_default'])} · "
            f"tgprev={int(flags['tg_preview'])} · {datetime.now(MOSCOW).strftime('%d.%m %H:%M')} МСК")
    lines = [head]
    for value, product, has_photo, events in results:
        sku = ""
        from storefront import sku_in_url
        sku = sku_in_url(getattr(product, "url", "")) if product is not None else ""
        if product is None:
            lines.append(f"- {short_text(value, 60)} · ошибка разбора")
            continue
        fields = "".join(("T" if product.title.strip() and not product.provisional_title else ("t" if product.title else "-"),
                          "P" if product.price is not None else ("x" if product.price_unavailable else "-"),
                          "F" if has_photo else ("f" if product.image_urls else "-")))
        lines.append(f"- {product.marketplace}:{sku} · {fields} · цена={product.price_source or '-'}"
                     f" · назв={product.title_source or '-'}")
        for event in events:
            code = f" {event.http}" if event.http else ""
            took = f" {event.ms}ms" if event.ms is not None else ""
            retry = f" retry@{moscow(event.retry_at)}" if event.retry_at else ""
            lines.append(f"  {event.source}:{event.outcome}{code}{took}{retry}")
    blocks = state.STATE.blocks()
    if blocks:
        lines.append("паузы: " + ", ".join(f"{k} до {moscow(v[0])}" for k, v in sorted(blocks.items())))
    return "\n".join(lines)
