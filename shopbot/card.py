"""Общая модель товара и сборка подписи для карточки в Telegram."""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal

MARKET_NAMES = {"wb": "Wildberries", "ozon": "Ozon"}
TITLE_MAX = 120
RUBLE_PRICE = re.compile(
    r"([0-9]{1,3}(?:[ \u00a0\u202f\u2009\u2007][0-9]{3})+|[0-9]+)"
    r"(?:[,.]([0-9]{1,2}))?(?:\s*(?:₽|руб(?:лей|ля|ль)?\.?|р\.?|RUB))?",
    re.IGNORECASE,
)


class ParseError(Exception):
    """Не удалось получить данные товара (текст показывается пользователю)."""


@dataclass
class Product:
    marketplace: str  # "wb" | "ozon"
    title: str
    url: str
    price: Decimal | int | None = None  # рубли с точностью до копеек
    old_price: Decimal | int | None = None
    image_urls: list[str] = field(default_factory=list)
    price_note: str = ""
    import_note: str = field(default="", compare=False, repr=False)
    # Условия цены и источник сохраняются в черновике вместе с основной суммой.
    price_source: str = field(default="", compare=False)
    conditional_price: Decimal | None = field(default=None, compare=False)
    conditional_note: str = field(default="", compare=False)
    price_unavailable: bool = field(default=False, compare=False)
    wb_dest: str = field(default="", compare=False)
    used_fallback: bool = field(default=False, compare=False)
    rating: float | None = None
    feedbacks: int | None = None
    # v10: происхождение каждого поля. Пустая строка — источник неизвестен (черновики до v10).
    title_source: str = field(default="", compare=False)
    photo_source: str = field(default="", compare=False)
    price_at: int | None = field(default=None, compare=False)  # когда источник показал эту цену, unix time
    reference_note: str = field(default="", compare=False)  # датированная справка, в пост не попадает
    gaps: dict = field(default_factory=dict, compare=False)  # поле → почему не заполнено
    trace: list = field(default_factory=list, compare=False, repr=False)  # события импорта, в базу не пишутся
    # Противоречивые значения из другого источника: {"price": {"value": "1299", "source": "message"}}.
    alternatives: dict = field(default_factory=dict, compare=False)

    @property
    def market_name(self) -> str:
        return MARKET_NAMES.get(self.marketplace, self.marketplace)

    @property
    def provisional_title(self) -> bool:
        """Название восстановлено из адреса ссылки и ждёт подтверждения администратора."""
        return self.title_source == PROVISIONAL_TITLE and bool(self.title.strip())


PROVISIONAL_TITLE = "ozon.slug"


def manual_price(product, amount, source="manual"):
    return replace(product, price=amount, old_price=None, price_note="", conditional_price=None,
                   conditional_note="", price_unavailable=False, price_source=source,
                   price_at=int(time.time()) if amount is not None else None,
                   gaps={k: v for k, v in product.gaps.items() if k != "price"})


def parse_price(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = html.unescape(str(value)).translate({0x200B: None, 0x2060: None, 0xFEFF: None})
    match = RUBLE_PRICE.fullmatch(text.strip())
    if match is None:
        return None
    whole = re.sub(r"\s", "", match.group(1))
    if len(whole) > 15:
        return None
    amount = Decimal(f"{whole}.{match.group(2) or '0'}")
    return amount if amount > 0 else None


def fmt_price(value: Decimal | int) -> str:
    whole, fraction = f"{value:,.2f}".split(".")
    number = whole.replace(",", "\u00a0")
    if fraction != "00":
        number += "," + fraction
    return number + "\u00a0₽"


def short_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    cut = encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore")
    return (cut.rpartition(" ")[0] or cut).rstrip() + "…"


def shorten_name(value: str) -> str:
    text = " ".join(value.split())
    first = re.split(r"\s*[,;|]\s*|\s+[—•]\s+", text, maxsplit=1)[0]
    if len(first) >= 10:
        text = first
    seen = set()
    words = []
    for word in text.split():
        key = word.strip(".,").casefold()
        if key and key not in seen:
            seen.add(key)
            words.append(word)
    return short_text(" ".join(words[:10]), 62)


def title_html(product: Product, limit: int = TITLE_MAX) -> str:
    title = short_text(product.title, limit) or f"Товар на {product.market_name}"
    url = html.escape(product.url, quote=True)
    return f'<a href="{url}">{html.escape(title)}</a>'


def price_html(product: Product) -> str:
    if not product.price:
        return ""

    price = f"<b>{fmt_price(product.price)}</b>"
    if product.price_note:
        price += f" · {html.escape(product.price_note)}"
    elif product.old_price and product.old_price > product.price:
        price += f"  <s>{fmt_price(product.old_price)}</s>"
    return price


def build_caption(product: Product) -> str:
    """Под фото стоят название-ссылка и цена."""
    return "\n".join(filter(None, (title_html(product), price_html(product))))
