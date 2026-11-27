"""Подпись и изображение для карточки или подборки товаров."""
from __future__ import annotations

import html
import io
import os
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from card import fmt_price, price_html, title_html, short_text
from drafts import Draft

def caption_units(markup: str) -> int:
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return len(text.encode("utf-16-le")) // 2


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
            body = "\n".join(filter(None, (title_html(product, cap), price_html(product))))
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


WIDTH, MARGIN, GUTTER = 1080, 28, 20
HEADER, FOOTER, SINGLE_FOOTER = 132, 152, 216
RADIUS = 30

# Палитра: тёплая бумага, глубокий зелёный бренда, коралловый для скидок.
PAPER_TOP, PAPER_BOTTOM = "#F9F5EC", "#EAE2D2"
INK, MUTED, GREEN, GOLD = "#14281F", "#6C7A71", "#12503F", "#E9B44C"
CORAL = "#E4414D"
MARKET_COLOR = {"wb": "#8B2DB5", "ozon": "#0A5CFF"}
MARKET_SHORT = {"wb": "WB", "ozon": "Ozon"}

LAYOUT_NAMES = {
    3: ("Акцент на первом", "Три в ряд"),
    5: ("Два сверху", "Три сверху"),
    6: ("Три в ряд", "Два в ряд"),
}

FONT_DIR = Path(__file__).with_name("fonts")


@lru_cache(maxsize=64)
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


def wrap_lines(text, face, width, lines=2):
    """Переносит название по словам. Если текст не поместился, последняя строка кончается на «…» после целого слова."""
    words = text.split()
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
        out[-1] = last.rstrip(" ,.;:-—") + "…"
    return out or [""]


def aspect(photo):
    if photo:
        with Image.open(io.BytesIO(photo)) as source:
            return min(1.65, max(.60, source.width / source.height))
    return .75


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
    canvas = ImageOps.colorize(gradient, PAPER_TOP, PAPER_BOTTOM).convert("RGB")
    # Два размытых пятна добавляют глубину. Считаются на уменьшенной копии, поэтому быстро.
    scale = 6
    glow = Image.new("RGBA", (width // scale, height // scale), (0, 0, 0, 0))
    paint = ImageDraw.Draw(glow)
    paint.ellipse((glow.width * .55, -glow.height * .08, glow.width * 1.25, glow.height * .22), fill=(18, 80, 63, 38))
    paint.ellipse((-glow.width * .3, glow.height * .78, glow.width * .35, glow.height * 1.12), fill=(233, 180, 76, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(14)).resize((width, height), Image.Resampling.BICUBIC)
    canvas.paste(glow, (0, 0), glow)
    return canvas


def drop_shadow(canvas, box, radius):
    x, y, w, h = box
    pad = 40
    layer = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    layer.paste(255, (pad, pad + 10), rounded_mask((w, h), radius))
    layer = layer.filter(ImageFilter.GaussianBlur(16)).point(lambda v: v * 0.22)
    canvas.paste((20, 40, 31), (x - pad, y - pad), layer)


def badges_enabled() -> bool:
    return os.getenv("IMAGE_BADGES", "1") != "0"


def discount_percent(p):
    if p.price and p.old_price and p.old_price > p.price and not p.price_note:
        percent = int(100 * (p.old_price - p.price) / p.old_price)
        if 0 < percent < 100:
            return percent
    return None


def draw_photo(tile, box, item):
    """Фото целиком, без обрезки. Без фото — спокойная заглушка."""
    x, y, w, h = box
    if item.photo:
        with Image.open(io.BytesIO(item.photo)) as original:
            with ImageOps.contain(original.convert("RGB"), (w, h), Image.Resampling.LANCZOS) as photo:
                tile.paste(photo, (x + (w - photo.width) // 2, y + (h - photo.height) // 2))
        return
    pill(tile, (x, y, x + w, y + h), "#E4EBE4", 0)
    draw = ImageDraw.Draw(tile)
    draw.text((x + w / 2, y + h / 2), "Добавьте фото", font=font(24, 600), fill=GREEN, anchor="mm")


def draw_overlays(tile, box, item, index):
    x, y, w, h = box
    draw = ImageDraw.Draw(tile)
    big = index is None
    if index is not None:
        size = 56
        pill(tile, (x + 16, y + 16, x + 16 + size, y + 16 + size), GREEN)
        draw.text((x + 16 + size / 2, y + 16 + size / 2 + 1), str(index), font=font(28, 800), fill="white", anchor="mm")
    percent = discount_percent(item.product)
    if percent and badges_enabled():
        face = font(40 if big else 26, 800)
        label = f"−{percent}%"
        text_w = round(face.getlength(label))
        pad_x, pad_y = (22, 11) if big else (15, 8)
        right, top = x + w - 16, y + 16
        pill(tile, (right - text_w - 2 * pad_x, top, right, top + face.size + 2 * pad_y), CORAL)
        draw.text((right - text_w / 2 - pad_x, top + (face.size + 2 * pad_y) / 2 + 1), label, font=face, fill="white", anchor="mm")


def draw_footer(tile, top, width, height, item, *, single):
    p = item.product
    draw = ImageDraw.Draw(tile)
    pad = 26 if single else 18
    inner = width - 2 * pad
    base = top + height - (30 if single else 26)

    # Метка площадки справа в строке цены. Размеры считаем заранее, чтобы цена её не перекрыла.
    chip_face = font(22 if single else 17, 800)
    name = MARKET_SHORT.get(p.marketplace, p.marketplace)
    chip_w = round(chip_face.getlength(name)) + (36 if single else 24)
    chip_h = round(chip_face.size * 1.75)
    chip_right = width - pad
    lift = 6 if single else 4
    chip_box = (chip_right - chip_w, base - chip_h + lift, chip_right, base + lift)

    label = fmt_price(p.price) if p.price else "Указать цену"
    price_color = GREEN if p.price else MUTED
    avail = inner - chip_w - 14
    size = 60 if single else 36
    while size > 16 and font(size, 800).getlength(label) > avail:
        size -= 2
    price_face = font(size, 800)
    price_w = price_face.getlength(label)

    # Условие цены («с Ozon Картой») нельзя терять: если оно не помещается в строке цены, идёт отдельной строкой.
    extra_face = font(22 if single else 17, 600)
    note = p.price_note
    room = avail - price_w - 14
    note_inline = bool(note) and extra_face.getlength(note) <= room
    title_lines = 1 if (note and not note_inline) else 2

    title_face = font(36 if single else 24, 700 if single else 600)
    line_h = round(title_face.size * 1.28)
    y = top + (18 if single else 14)
    for line in wrap_lines(p.title or "Добавьте название", title_face, inner, title_lines):
        draw.text((pad, y), line, font=title_face, fill=INK)
        y += line_h
    if note and not note_inline:
        draw.text((pad, y + 2), fit_line(note, extra_face, inner), font=extra_face, fill=MUTED)

    draw.text((pad, base), label, font=price_face, fill=price_color, anchor="ls")
    if badges_enabled():
        pill(tile, chip_box, MARKET_COLOR.get(p.marketplace, GREEN))
        draw.text(((chip_box[0] + chip_box[2]) / 2, (chip_box[1] + chip_box[3]) / 2 + 1), name,
                  font=chip_face, fill="white", anchor="mm")
    else:
        draw.text((chip_right, base), name, font=chip_face, fill=MUTED, anchor="rs")

    x0 = pad + price_w + 14
    if note_inline:
        draw.text((x0, base), note, font=extra_face, fill=MUTED, anchor="ls")
    elif not note and p.price and p.old_price and p.old_price > p.price:
        old = fmt_price(p.old_price)
        if extra_face.getlength(old) <= room:
            draw.text((x0, base), old, font=extra_face, fill=MUTED, anchor="ls")
            mid = base - extra_face.size * .32
            draw.line((x0, mid, x0 + extra_face.getlength(old), mid), fill=MUTED, width=2)


def draw_tile(canvas, box, item, index=None):
    """Карточка товара: тень, скруглённые углы, фото целиком, подпись и цена."""
    x, y, w, h = box
    single = index is None
    footer = SINGLE_FOOTER if single else FOOTER
    total = (w, h + footer)
    radius = RADIUS + (6 if single else 0)
    drop_shadow(canvas, (x, y, *total), radius)
    tile = Image.new("RGB", total, "white")
    draw_photo(tile, (0, 0, w, h), item)
    draw_overlays(tile, (0, 0, w, h), item, index)
    ImageDraw.Draw(tile).line((22, h, w - 22, h), fill="#EEF0E9", width=2)
    draw_footer(tile, h, w, footer, item, single=single)
    canvas.paste(tile, (x, y), rounded_mask(total, radius))


def layout_boxes(ratios, mode):
    """Возвращает позиции фото в порядке товаров. Подпись занимает FOOTER под каждым фото."""
    n = len(ratios)
    left, top = MARGIN, HEADER
    width = WIDTH - 2 * MARGIN
    if n == 3 and mode == "auto":
        first, second, third = ratios
        right = round(((width-GUTTER)/first - FOOTER-GUTTER) / (1/first + 1/second + 1/third))
        right = min(width//2, max(300, right))
        hero = width - GUTTER - right
        second_h, third_h = round(right/second), round(right/third)
        full = second_h + third_h + FOOTER*2 + GUTTER
        rx = left + hero + GUTTER
        return [(left, top, hero, full-FOOTER),
                (rx, top, right, second_h),
                (rx, top+second_h+FOOTER+GUTTER, right, third_h)]
    rows = {2:(2,), 3:(3,), 4:(2,2), 5:(2,3), 6:(3,3)}[n]
    if mode == "alternate":
        if n == 5:
            rows = (3,2)
        elif n == 6:
            rows = (2,2,2)
    boxes = []
    for count in rows:
        row = ratios[len(boxes):len(boxes)+count]
        usable = width-GUTTER*(count-1)
        h = round(usable/sum(row))
        x = left
        for j, ratio in enumerate(row):
            w = round(usable*ratio/sum(row)) if j < count-1 else WIDTH-MARGIN-x
            boxes.append((x, top, w, h))
            x += w+GUTTER
        top += h+FOOTER+GUTTER
    return boxes


def draw_header(canvas, draft):
    draw = ImageDraw.Draw(canvas)
    heading = draft.heading if draft.heading and draft.heading != "Подборка BorzMarket" else "Подборка товаров"
    count = len(draft.items)
    brand_face = font(26, 800)
    brand_w = round(brand_face.getlength("BorzMarket")) + 44
    right = WIDTH - MARGIN
    pill(canvas, (right - brand_w, 36, right, 36 + 54), GREEN)
    draw.text((right - brand_w / 2, 36 + 28), "BorzMarket", font=brand_face, fill="white", anchor="mm")
    draw.ellipse((MARGIN + 4, 100, MARGIN + 16, 112), fill=GOLD)
    title_face = font(46, 800)
    draw.text((MARGIN + 4, 30), fit_line(heading, title_face, WIDTH - 2 * MARGIN - brand_w - 30), font=title_face, fill=INK)
    sub = f"{count} {plural_goods(count)} · цены и ссылки под фото"
    draw.text((MARGIN + 26, 106), sub, font=font(22, 600), fill=MUTED, anchor="lm")


def plural_goods(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "товаров"
    return {1: "товар", 2: "товара", 3: "товара", 4: "товара"}.get(count % 10, "товаров")


def post_photo(draft: Draft) -> bytes | None:
    if len(draft.items) == 1:
        item = draft.items[0]
        if not item.photo or not badges_enabled():
            return item.photo
        inner = WIDTH - 2 * MARGIN
        photo_h = round(inner / aspect(item.photo))
        height = photo_h + SINGLE_FOOTER + 2 * MARGIN
        with background(WIDTH, height) as canvas:
            draw_tile(canvas, (MARGIN, MARGIN, inner, photo_h), item)
            buffer = io.BytesIO()
            canvas.save(buffer, "JPEG", quality=92, optimize=True)
            return buffer.getvalue()
    boxes = layout_boxes([aspect(item.photo) for item in draft.items], draft.layout)
    height = max(y+h+FOOTER for _,y,_,h in boxes) + MARGIN
    with background(WIDTH, height) as canvas:
        draw_header(canvas, draft)
        for index, (box, item) in enumerate(zip(boxes, draft.items), 1):
            draw_tile(canvas, box, item, index)
        buffer = io.BytesIO()
        canvas.save(buffer, "JPEG", quality=92, optimize=True)
        return buffer.getvalue()
