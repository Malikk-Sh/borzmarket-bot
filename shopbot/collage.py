"""Подпись и изображение для карточки или подборки товаров.

v10. Квадратный холст 1080×1080: Telegram для Android показывает фото с шириной не меньше
высоты почти во всю ширину экрана, а вертикальное — на 70 % ширины и ещё уменьшает его по
высоте (ChatMessageCell.getMessageSize в github.com/DrKLO/Telegram). Поэтому коллаж квадратный,
а место внутри отдано товару: однородные поля вокруг товара на фото площадки обрезаются,
«живые» кадры не режутся и вписываются на размытую подложку того же снимка.
"""
from __future__ import annotations

import hashlib
import html
import io
import math
import os
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from card import fmt_price, price_html, title_html, short_text
from drafts import Draft


def caption_units(markup: str) -> int:
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return len(text.encode("utf-16-le")) // 2


def condition_line(product) -> str:
    """Вторая цена с условием оплаты: «С банками — 3 608 ₽». Пусто, если её нет."""
    if (product.conditional_price and product.price and product.conditional_price != product.price
            and product.conditional_note and not product.price_note):
        return f"{product.conditional_note} — {fmt_price(product.conditional_price)}"
    return ""


def post_caption(draft: Draft, urls: tuple[str, ...] | None = None, *, reserve: int = 0) -> str:
    products = [item.product for item in draft.items]
    if urls is not None:
        products = [replace(product, url=url) for product, url in zip(products, urls)]

    tail = []
    if draft.comment:
        tail.append(f"<blockquote>{html.escape(short_text(draft.comment, 120))}</blockquote>")
    if draft.tags:
        tail.append(html.escape(" ".join(draft.tags)))
    if draft.erid:
        tail.append(f"erid {html.escape(draft.erid)}")
    suffix = "\n\n" + "\n".join(tail) if tail else ""

    cap = 120 if len(products) == 1 else 66
    while True:
        if len(products) == 1:
            product = products[0]
            extra = condition_line(product)
            body = "\n".join(filter(None, (title_html(product, cap), price_html(product),
                                           html.escape(extra) if extra else "")))
        else:
            heading = html.escape(short_text(draft.heading, 64))
            rows = [
                f"{index}. {title_html(product, cap)} · {price_html(product) or 'цена не указана'}"
                for index, product in enumerate(products, 1)
            ]
            body = f"<b>{heading}</b>\n\n" + "\n".join(rows)

        caption = body + suffix
        excess = caption_units(caption) + reserve - 1024
        if excess <= 0 or cap == 12:
            return caption
        cap = max(12, cap - (excess + len(products) - 1) // len(products))


WIDTH = HEIGHT = 1080
MARGIN, GUTTER = 24, 16
RADIUS = 28
HEADER = 76  # полоса заголовка, только если администратор задал свой заголовок

# Палитра: тёплая бумага, глубокий зелёный бренда, коралловый для скидок.
PAPER_TOP, PAPER_BOTTOM = "#F7F3EA", "#ECE4D4"
INK, MUTED, GREEN, GOLD = "#14281F", "#6C7A71", "#12503F", "#E9B44C"
CORAL = "#E4414D"
WARN = "#B8860B"
MARKET_COLOR = {"wb": "#8B2DB5", "ozon": "#0A5CFF"}
MARKET_SHORT = {"wb": "WB", "ozon": "Ozon"}
DEFAULT_HEADINGS = {"", "Подборка BorzMarket", "Подборка товаров"}

LAYOUT_NAMES = {
    3: ("Акцент на первом", "Три в ряд"),
    5: ("Два сверху", "Три сверху"),
    6: ("Три в ряд", "Два в ряд"),
}

FONT_DIR = Path(__file__).with_name("fonts")


@lru_cache(maxsize=96)
def font(size: int, weight: int = 500):
    """Manrope нужного веса (400–800). Без файла или без поддержки осей работает DejaVu."""
    try:
        face = ImageFont.truetype(str(FONT_DIR / "Manrope-Variable.ttf"), size)
        face.set_variation_by_axes([max(200, min(800, weight))])
        return face
    except (OSError, ValueError, AttributeError):
        name = "DejaVuSans-Bold.ttf" if weight >= 650 else "DejaVuSans.ttf"
        return ImageFont.truetype(str(FONT_DIR / name), size)


def fit_line(text, face, width):
    text = short_text(text, 130)
    if face.getlength(text) <= width:
        return text
    while text and face.getlength(text.rstrip() + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


_UNIT = re.compile(r"(\d)\s+(мл|л|кг|г|см|мм|м|кВт|Вт|В|ГБ|ТБ|шт|бар|%|₽)(?=$|[\s,.;:/)])")


def wrap_lines(text, face, width, lines=2):
    """Переносит название по словам. Если текст не поместился, последняя строка кончается на «…» после целого слова.

    Число и единица («30 л», «15 бар») не разрываются переносом.
    """
    text = _UNIT.sub("\\1\u00a0\\2", " ".join(text.split()))
    words = [word for word in text.split(" ") if word]
    out, i = [], 0
    while i < len(words) and len(out) < lines:
        line = ""
        while i < len(words):
            trial = f"{line} {words[i]}".strip()
            if face.getlength(trial) > width:
                break
            line, i = trial, i + 1
        if not line:  # одно слово шире строки
            line, i = fit_line(words[i], face, width), i + 1
        out.append(line)
    if i < len(words) and out:
        last = out[-1]
        while last and face.getlength(last + "…") > width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        out[-1] = last.rstrip(" ,.;:-—/") + "…"
    return out or [""]


@lru_cache(maxsize=64)
def rounded_mask(size: tuple[int, int], radius: int, scale: int = 3):
    """Маска со скруглёнными углами без «лесенки»: рисуем крупнее и уменьшаем."""
    big = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=radius * scale, fill=255)
    return big.resize(size, Image.Resampling.LANCZOS)


def pill(image, box, fill, radius=None):
    left, top, right, bottom = (round(v) for v in box)
    size = (max(2, right - left), max(2, bottom - top))
    mask = rounded_mask(size, min(radius if radius is not None else size[1] // 2, size[1] // 2))
    image.paste(fill, (left, top), mask)


def background(width, height):
    gradient = Image.linear_gradient("L").resize((width, height))
    return ImageOps.colorize(gradient, PAPER_TOP, PAPER_BOTTOM).convert("RGB")


def drop_shadow(canvas, box, radius):
    x, y, w, h = box
    pad = 30
    layer = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    layer.paste(255, (pad, pad + 8), rounded_mask((w, h), radius))
    layer = layer.filter(ImageFilter.GaussianBlur(12)).point(lambda v: v * 0.16)
    canvas.paste((20, 40, 31), (x - pad, y - pad), layer)


def badges_enabled() -> bool:
    return os.getenv("IMAGE_BADGES", "1") != "0"


def discount_percent(p):
    if p.price and p.old_price and p.old_price > p.price and not p.price_note:
        percent = int(100 * (p.old_price - p.price) / p.old_price)
        if 0 < percent < 100:
            return percent
    return None


# --- фото товара: поиск однородных полей ----------------------------------------------------
@dataclass(frozen=True)
class PhotoInfo:
    image: Image.Image
    box: tuple[int, int, int, int]  # область с товаром (с запасом)
    background: tuple[int, int, int] | None  # цвет однородного фона или None для «живого» кадра

    @property
    def aspect(self) -> float:
        left, top, right, bottom = self.box
        return max(0.2, min(5.0, (right - left) / max(1, bottom - top)))


TRIM_TOLERANCE = 18  # отличие пикселя от фона, после которого он считается товаром
BORDER_SHARE = 0.93  # такая доля краёв кадра должна совпасть с фоном


def _analyze(data: bytes) -> PhotoInfo:
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    probe = image.copy()
    probe.thumbnail((220, 220))
    w, h = probe.size
    pixels = probe.load()
    ring = max(1, min(w, h) // 40)
    border = [pixels[x, y] for x in range(w) for y in (*range(ring), *range(h - ring, h))]
    border += [pixels[x, y] for y in range(h) for x in (*range(ring), *range(w - ring, w))]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    close = sum(max(abs(c[i] - bg[i]) for i in range(3)) <= TRIM_TOLERANCE for c in border) / len(border)
    full = (0, 0, image.width, image.height)
    if close < BORDER_SHARE:
        return PhotoInfo(image, full, None)
    diff = ImageChops.difference(probe, Image.new("RGB", probe.size, bg))
    r, g, b = diff.split()
    strongest = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mask = strongest.point(lambda v: 255 if v > TRIM_TOLERANCE else 0).filter(ImageFilter.MaxFilter(3))
    found = mask.getbbox()
    if not found:
        return PhotoInfo(image, full, bg)  # пустой кадр — показываем как есть
    sx, sy = image.width / w, image.height / h
    left, top, right, bottom = found[0] * sx, found[1] * sy, found[2] * sx, found[3] * sy
    margin = 0.04 * max(right - left, bottom - top)  # запас, чтобы не задеть край товара
    box = (max(0, math.floor(left - margin)), max(0, math.floor(top - margin)),
           min(image.width, math.ceil(right + margin)), min(image.height, math.ceil(bottom + margin)))
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.94 * image.width * image.height:
        box = full
    return PhotoInfo(image, box, bg)


_ANALYSIS: dict[str, PhotoInfo] = {}


def analyze(data: bytes) -> PhotoInfo:
    key = hashlib.sha1(data).hexdigest()
    info = _ANALYSIS.get(key)
    if info is None:
        info = _analyze(data)
        if len(_ANALYSIS) > 48:
            _ANALYSIS.pop(next(iter(_ANALYSIS)))
        _ANALYSIS[key] = info
    return info


def aspect(photo):
    """Пропорции товара на фото (после обрезки однородных полей)."""
    if photo:
        try:
            return analyze(photo).aspect
        except (OSError, ValueError, Image.DecompressionBombError):
            return .75
    return .75


def place_photo(tile, box, photo, *, top_reserve: int = 0):
    """Товар целиком и крупно. Однородный фон продолжается, живой кадр ложится на размытую подложку.

    top_reserve — полоса сверху под номер товара: обрезанный товар опускается ниже значка.
    """
    x, y, w, h = box
    if not photo:
        draw_placeholder(tile, box)
        return
    info = analyze(photo)
    if info.background is not None:
        tile.paste(info.background, (x, y, x + w, y + h))
        pad = round(0.06 * min(w, h))
        top = max(pad, top_reserve)
        content = info.image.crop(info.box)
        scale = min((w - 2 * pad) / content.width, (h - top - pad) / content.height, 3.0)
        size = (max(1, round(content.width * scale)), max(1, round(content.height * scale)))
        with content.resize(size, Image.Resampling.LANCZOS) as fitted:
            tile.paste(fitted, (x + (w - size[0]) // 2, y + top + (h - top - pad - size[1]) // 2))
        return
    image = info.image
    ratio, area = image.width / image.height, w / h
    if abs(math.log(ratio / area)) <= 0.10:
        # Пропорции почти совпадают: заполняем рамку, срез не больше ~10 %.
        with ImageOps.fit(image, (w, h), Image.Resampling.LANCZOS) as filled:
            tile.paste(filled, (x, y))
        return
    small = image.copy()
    small.thumbnail((96, 96))
    with ImageOps.fit(small, (max(1, w // 8), max(1, h // 8))) as cover:
        blurred = cover.filter(ImageFilter.GaussianBlur(3)).resize((w, h), Image.Resampling.BICUBIC)
    blurred = Image.blend(blurred, Image.new("RGB", (w, h), (245, 240, 230)), 0.35)
    tile.paste(blurred, (x, y))
    with ImageOps.contain(image, (w, h), Image.Resampling.LANCZOS) as fitted:
        tile.paste(fitted, (x + (w - fitted.width) // 2, y + (h - fitted.height) // 2))


def draw_placeholder(tile, box):
    x, y, w, h = box
    tile.paste("#F1EEE6", (x, y, x + w, y + h))
    draw = ImageDraw.Draw(tile)
    inset = max(10, min(w, h) // 14)
    draw.rounded_rectangle((x + inset, y + inset, x + w - inset, y + h - inset), radius=18,
                           outline="#C9C2B2", width=3)
    face = font(max(22, min(36, w // 11)), 700)
    draw.text((x + w / 2, y + h / 2), "Добавьте фото", font=face, fill=WARN, anchor="mm")


# --- подписи на плитке ---------------------------------------------------------------------
@dataclass(frozen=True)
class Style:
    title: int
    title_weight: int
    lines: int
    price: int
    old: int
    note: int
    chip: int
    number: int
    pad: int


STYLES = {
    "xl": Style(46, 700, 3, 96, 38, 34, 28, 0, 34),
    "l": Style(40, 700, 2, 70, 32, 29, 24, 74, 22),
    "m": Style(36, 700, 2, 60, 29, 27, 22, 68, 18),
    "s": Style(32, 700, 2, 52, 26, 25, 20, 62, 16),
}


def style_for(width: int) -> Style:
    if width >= 900:
        return STYLES["xl"]
    if width >= 560:
        return STYLES["l"]
    if width >= 400:
        return STYLES["m"]
    return STYLES["s"]


def footer_height(item, width: int, style: Style) -> int:
    p = item.product
    inner = width - 2 * style.pad
    title_face = font(style.title, style.title_weight)
    lines = len(wrap_lines(p.title or "Добавьте название", title_face, inner, style.lines))
    height = style.pad + lines * round(style.title * 1.22) + 8 + round(style.price * 1.08) + style.pad - 4
    if p.price_note or condition_line(p):
        height += round(style.note * 1.3)
    return height


def draw_footer(tile, box, item, style: Style):
    x, y, w, h = box
    p = item.product
    draw = ImageDraw.Draw(tile)
    pad = style.pad
    inner = w - 2 * pad
    title_face = font(style.title, style.title_weight)
    line_h = round(style.title * 1.22)
    top = y + pad - 2
    color = INK if p.title and not p.provisional_title else WARN
    for line in wrap_lines(p.title or "Добавьте название", title_face, inner, style.lines):
        draw.text((x + pad, top), line, font=title_face, fill=color)
        top += line_h

    chip_face = font(style.chip, 800)
    name = MARKET_SHORT.get(p.marketplace, p.marketplace)
    chip_w = round(chip_face.getlength(name)) + style.chip
    chip_h = round(style.chip * 1.6)
    label = fmt_price(p.price) if p.price else "Указать цену"
    size = style.price if p.price else round(style.price * 0.62)
    avail = inner - chip_w - 12
    while size > 18 and font(size, 800).getlength(label) > avail:
        size -= 2
    price_face = font(size, 800)
    base = top + 8 + round(style.price * 0.86)
    draw.text((x + pad, base), label, font=price_face, fill=GREEN if p.price else WARN, anchor="ls")
    chip_box = (x + w - pad - chip_w, base - chip_h + 2, x + w - pad, base + 2)
    if badges_enabled():
        pill(tile, chip_box, MARKET_COLOR.get(p.marketplace, GREEN))
        draw.text(((chip_box[0] + chip_box[2]) / 2, (chip_box[1] + chip_box[3]) / 2 + 1), name,
                  font=chip_face, fill="white", anchor="mm")
    else:
        draw.text((x + w - pad, base), name, font=chip_face, fill=MUTED, anchor="rs")

    # Зачёркнутая цена — в строку, если помещается. Условие оплаты не теряется никогда.
    old_face = font(style.old, 600)
    x0 = x + pad + price_face.getlength(label) + 12
    room = chip_box[0] - 10 - x0
    if p.price and not p.price_note and p.old_price and p.old_price > p.price:
        old = fmt_price(p.old_price)
        if old_face.getlength(old) <= room:
            draw.text((x0, base), old, font=old_face, fill=MUTED, anchor="ls")
            mid = base - style.old * .32
            draw.line((x0, mid, x0 + old_face.getlength(old), mid), fill=MUTED, width=2)
    draw_condition(draw, p, x + pad, base + round(style.note * 1.25), inner, style.note)


def draw_condition(draw, p, left, baseline, width, size):
    """«39 900 ₽ с WB Кошельком»: сумма не обрезается, при нехватке места сокращается условие."""
    if not p.price:
        return
    if p.price_note:
        draw.text((left, baseline), fit_line(p.price_note, font(size, 650), width), font=font(size, 650),
                  fill=MUTED, anchor="ls")
        return
    if not condition_line(p):
        return
    amount = fmt_price(p.conditional_price)
    strong = font(size, 800)
    draw.text((left, baseline), amount, font=strong, fill=INK, anchor="ls")
    rest = width - strong.getlength(amount) - 8
    note = p.conditional_note[:1].lower() + p.conditional_note[1:]
    if rest > size * 2:
        draw.text((left + strong.getlength(amount) + 8, baseline), fit_line(note, font(size, 600), rest),
                  font=font(size, 600), fill=MUTED, anchor="ls")


def draw_badges(tile, box, item, index, style: Style):
    x, y, w, h = box
    draw = ImageDraw.Draw(tile)
    if index is not None:
        size = style.number
        left, top = x + 14, y + 14
        pill(tile, (left, top, left + size, top + size), GREEN)
        draw.text((left + size / 2, top + size / 2 + 1), str(index), font=font(round(size * 0.56), 800),
                  fill="white", anchor="mm")
    percent = discount_percent(item.product)
    if percent and badges_enabled():
        face = font(round(style.price * 0.5), 800)
        label = f"−{percent}%"
        text_w = round(face.getlength(label))
        pad_x, pad_y = round(face.size * .45), round(face.size * .22)
        right, top = x + w - 14, y + 14
        pill(tile, (right - text_w - 2 * pad_x, top, right, top + face.size + 2 * pad_y), CORAL)
        draw.text((right - text_w / 2 - pad_x, top + (face.size + 2 * pad_y) / 2 + 1), label, font=face,
                  fill="white", anchor="mm")


def contain_area(ratio: float, width: float, height: float) -> float:
    """Площадь товара с пропорциями ratio, вписанного в рамку."""
    if width <= 0 or height <= 0:
        return 0.0
    return min(width, height * ratio) * min(height, width / ratio)


def tile_mode(w: int, h: int, item, style: Style, index) -> tuple[str, int]:
    """Сверху подпись или сбоку: выбирается компоновка, где сам товар крупнее.

    Высокий товар в широкой плитке ставится слева от текста, широкий — над текстом.
    Возвращает режим и ширину фото для бокового режима.
    """
    if not item.photo:
        return "stack", w
    ratio = aspect(item.photo)
    if ratio >= 0.7:
        return "stack", w  # сбоку подпись нужна только высоким товарам: термос, пароочиститель
    pad = round(0.06 * min(w, h))
    reserve = round(style.number * 0.55) + 14 if index is not None else 0
    footer = min(footer_height(item, w, style), h // 2)
    stacked = contain_area(ratio, w - 2 * pad, h - footer - max(pad, reserve) - pad)
    text_w = max(200, round(w * 0.46))
    photo_w = w - text_w
    if photo_w < 140:
        return "stack", w
    side = contain_area(ratio, photo_w - 2 * pad, h - max(pad, reserve) - pad)
    return ("side", photo_w) if side > stacked * 1.3 else ("stack", w)


def draw_tile(canvas, box, item, index=None):
    """Плитка товара: товар крупно, название и цена снизу или сбоку — где товару больше места."""
    x, y, w, h = box
    style = style_for(w)
    tile = Image.new("RGB", (w, h), "white")
    reserve = round(style.number * 0.55) + 14 if index is not None else 0
    mode, photo_w = tile_mode(w, h, item, style, index)
    if mode == "side":
        place_photo(tile, (0, 0, photo_w, h), item.photo, top_reserve=reserve)
        draw_badges(tile, (0, 0, photo_w, h), item, index, style)
        draw_side_text(tile, (photo_w, 0, w - photo_w, h), item, STYLES["s"])
    else:
        footer = min(footer_height(item, w, style), h // 2)
        place_photo(tile, (0, 0, w, h - footer), item.photo, top_reserve=reserve)
        draw_badges(tile, (0, 0, w, h - footer), item, index, style)
        draw_footer(tile, (0, h - footer, w, footer), item, style)
    drop_shadow(canvas, (x, y, w, h), RADIUS)
    canvas.paste(tile, (x, y), rounded_mask((w, h), RADIUS))


def draw_single(canvas, item):
    """Одна карточка на весь квадрат. Высокий товар стоит слева от текста, широкий — над ним."""
    x, y, w, h = MARGIN, MARGIN, WIDTH - 2 * MARGIN, HEIGHT - 2 * MARGIN
    style = STYLES["xl"]
    tile = Image.new("RGB", (w, h), "white")
    ratio = aspect(item.photo) if item.photo else 1.0
    if item.photo and ratio < 0.82:
        photo_w = round(w * 0.56)
        place_photo(tile, (0, 0, photo_w, h), item.photo)
        draw_badges(tile, (0, 0, photo_w, h), item, None, style)
        draw_side_text(tile, (photo_w, 0, w - photo_w, h), item)
    else:
        footer = footer_height(item, w, style)
        place_photo(tile, (0, 0, w, h - footer), item.photo)
        draw_badges(tile, (0, 0, w, h - footer), item, None, style)
        draw_footer(tile, (0, h - footer, w, footer), item, style)
    drop_shadow(canvas, (x, y, w, h), RADIUS + 6)
    canvas.paste(tile, (x, y), rounded_mask((w, h), RADIUS + 6))


def draw_side_text(tile, box, item, style: Style | None = None):
    """Текстовая колонка справа от высокого товара: название сверху, крупная цена внизу."""
    x, y, w, h = box
    p = item.product
    draw = ImageDraw.Draw(tile)
    big = style is None
    title_size = 44 if big else style.title
    price_size = 96 if big else style.price
    note_size = 32 if big else style.note
    old_size = 38 if big else style.old
    pad = 36 if big else max(14, style.pad)
    inner = w - 2 * pad
    title_face = font(title_size, 700)
    line_h = round(title_size * 1.22)
    room_lines = max(1, int((h - 2 * pad - price_size * 2.4) // line_h))
    top = y + pad + (6 if big else 0)
    color = INK if p.title and not p.provisional_title else WARN
    for line in wrap_lines(p.title or "Добавьте название", title_face, inner, min(5, room_lines)):
        draw.text((x + pad, top), line, font=title_face, fill=color)
        top += line_h
    chip_size = 26 if big else style.chip
    chip_face = font(chip_size, 800)
    name = MARKET_SHORT.get(p.marketplace, p.marketplace)
    chip_w = round(chip_face.getlength(name)) + chip_size + 4
    chip_h = round(chip_size * 1.6)
    if badges_enabled():
        pill(tile, (x + pad, top + 10, x + pad + chip_w, top + 10 + chip_h), MARKET_COLOR.get(p.marketplace, GREEN))
        draw.text((x + pad + chip_w / 2, top + 10 + chip_h / 2 + 1), name, font=chip_face, fill="white", anchor="mm")
    else:
        draw.text((x + pad, top + 10 + chip_h / 2), name, font=chip_face, fill=MUTED, anchor="lm")
    bottom = y + h - pad
    if p.price and (p.price_note or condition_line(p)):
        draw_condition(draw, p, x + pad, bottom, inner, note_size)
        bottom -= round(note_size * 1.6)
    label = fmt_price(p.price) if p.price else "Указать цену"
    size = price_size if p.price else round(price_size * 0.6)
    while size > 18 and font(size, 800).getlength(label) > inner:
        size -= 2
    draw.text((x + pad, bottom), label, font=font(size, 800), fill=GREEN if p.price else WARN, anchor="ls")
    if p.price and not p.price_note and p.old_price and p.old_price > p.price:
        old_face = font(old_size, 600)
        old = fmt_price(p.old_price)
        if old_face.getlength(old) <= inner:
            top_old = bottom - size - round(old_size * 0.3)
            draw.text((x + pad, top_old), old, font=old_face, fill=MUTED, anchor="ls")
            mid = top_old - old_size * .32
            draw.line((x + pad, mid, x + pad + old_face.getlength(old), mid), fill=MUTED, width=2 if not big else 3)


# --- раскладка подборки ---------------------------------------------------------------------
def _rows(spec, left, top, width, height):
    boxes = []
    row_h = (height - GUTTER * (len(spec) - 1)) / len(spec)
    for r, count in enumerate(spec):
        usable = width - GUTTER * (count - 1)
        tile_w = usable / count
        y = top + r * (row_h + GUTTER)
        for c in range(count):
            x = left + c * (tile_w + GUTTER)
            right = left + width if c == count - 1 else x + tile_w
            boxes.append((round(x), round(y), round(right) - round(x), round(row_h)))
    return boxes


def estimated_area(ratio: float, w: int, h: int) -> float:
    """Оценка площади товара в плитке: лучшее из «подпись снизу» и «подпись сбоку»."""
    pad = 0.06 * min(w, h)
    footer = 190 if w < 400 else 210
    stacked = contain_area(ratio, w - 2 * pad, h - footer - 2 * pad - 30)
    text_w = max(190, w * 0.44)
    side = contain_area(ratio, w - text_w - 2 * pad, h - 2 * pad - 30) if w - text_w >= 140 else 0
    return max(stacked, side, 1.0)


def best_rows(candidates, ratios, left, top, width, height):
    """Из нескольких сеток выбирает ту, где товары в сумме крупнее (сумма логарифмов площадей)."""
    best, best_score = None, None
    for spec in candidates:
        boxes = _rows(spec, left, top, width, height)
        score = sum(math.log(estimated_area(r, w, h)) for r, (_, _, w, h) in zip(ratios, boxes))
        if best_score is None or score > best_score + 1e-9:
            best, best_score = boxes, score
    return best


def layout_boxes(ratios, mode="auto", *, top=MARGIN):
    """Плитки товаров (x, y, ширина, высота) в порядке номеров. Все внутри квадрата 1080."""
    n = len(ratios)
    left, width = MARGIN, WIDTH - 2 * MARGIN
    height = HEIGHT - MARGIN - top
    if n <= 1:
        return [(left, top, width, height)]
    if n == 2:
        return best_rows(((2,), (1, 1)), ratios, left, top, width, height)
    if n == 3:
        if mode == "alternate":
            return _rows((3,), left, top, width, height)
        hero = round((width - GUTTER) * 0.58)
        right_w = width - GUTTER - hero
        half = (height - GUTTER) // 2
        return [(left, top, hero, height), (left + hero + GUTTER, top, right_w, half),
                (left + hero + GUTTER, top + half + GUTTER, right_w, height - half - GUTTER)]
    if n == 4:
        return _rows((2, 2), left, top, width, height)
    if n == 5:
        return _rows((3, 2) if mode == "alternate" else (2, 3), left, top, width, height)
    return _rows((2, 2, 2) if mode == "alternate" else (3, 3), left, top, width, height)


def draw_header(canvas, draft) -> int:
    """Заголовок рисуется, только если администратор задал свой. Возвращает верх плиток."""
    heading = (draft.heading or "").strip()
    if heading in DEFAULT_HEADINGS:
        return MARGIN
    draw = ImageDraw.Draw(canvas)
    brand_face = font(22, 800)
    brand_w = round(brand_face.getlength("BorzMarket")) + 32
    right = WIDTH - MARGIN
    pill(canvas, (right - brand_w, MARGIN + 14, right, MARGIN + 54), GREEN)
    draw.text((right - brand_w / 2, MARGIN + 35), "BorzMarket", font=brand_face, fill="white", anchor="mm")
    title_face = font(44, 800)
    draw.text((MARGIN + 6, MARGIN + 36), fit_line(heading, title_face, WIDTH - 2 * MARGIN - brand_w - 24),
              font=title_face, fill=INK, anchor="lm")
    return MARGIN + HEADER


def plural_goods(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "товаров"
    return {1: "товар", 2: "товара", 3: "товара", 4: "товара"}.get(count % 10, "товаров")


def encode(canvas) -> bytes:
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=90, optimize=True, progressive=True)
    return buffer.getvalue()


def post_photo(draft: Draft) -> bytes | None:
    if len(draft.items) == 1:
        item = draft.items[0]
        if not item.photo or not badges_enabled():
            return item.photo  # IMAGE_BADGES=0 оставляет исходное фото без оформления
        with background(WIDTH, HEIGHT) as canvas:
            draw_single(canvas, item)
            return encode(canvas)
    with background(WIDTH, HEIGHT) as canvas:
        top = draw_header(canvas, draft)
        boxes = layout_boxes([aspect(item.photo) for item in draft.items], draft.layout, top=top)
        for index, (box, item) in enumerate(zip(boxes, draft.items), 1):
            draw_tile(canvas, box, item, index)
        return encode(canvas)
