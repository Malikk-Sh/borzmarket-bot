"""Как пост выглядит у подписчика в ленте канала на телефоне.

Ширина экрана 412 dp, картинка поста — «экран − 74 dp» для фото с шириной ≥ высоты и
70 % экрана для вертикального (ChatMessageCell.getMessageSize, github.com/DrKLO/Telegram,
ветка master, коммит 21f7d10). Масштаб 1 dp = 1 px, поэтому файл открывается «как на телефоне».

    .venv/bin/python tools/phone_mock.py examples/post_6.jpg examples/caption_6.html out.jpg
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).resolve().parent.parent / "fonts"
SCREEN = 412
SHARE_BUTTON = 10


def display_size(w: int, h: int, screen: int = SCREEN) -> tuple[int, int]:
    if w >= h:
        width = screen - 64 - SHARE_BUTTON
    else:
        width = int(screen * 0.7)
    max_h = width + 100
    scale = w / width
    dw, dh = width, int(h / scale)
    if dh > max_h:
        dw, dh = int(dw * max_h / dh), max_h
    return dw, dh


TOKEN = re.compile(r"(<[^>]+>)")


def runs(markup: str):
    """Разбивает HTML-подпись Telegram на куски текста со стилем: жирный, ссылка, зачёркнутый."""
    bold = link = strike = quote = False
    for part in TOKEN.split(markup):
        if not part:
            continue
        tag = part.lower()
        if tag.startswith("<b"):
            bold = True
        elif tag == "</b>":
            bold = False
        elif tag.startswith("<a "):
            link = True
        elif tag == "</a>":
            link = False
        elif tag == "<s>":
            strike = True
        elif tag == "</s>":
            strike = False
        elif tag.startswith("<blockquote"):
            quote = True
        elif tag == "</blockquote>":
            quote = False
        elif part.startswith("<"):
            continue
        else:
            yield html.unescape(part), bold, link, strike


def layout(markup: str, width: int, faces):
    """Перенос по словам с сохранением стиля. Возвращает строки из кусков (текст, стиль)."""
    lines, line, used = [], [], 0.0
    for text, bold, link, strike in runs(markup):
        for chunk in re.split(r"(\n| )", text):
            if chunk == "\n":
                lines.append(line)
                line, used = [], 0.0
                continue
            if not chunk:
                continue
            face = faces[bold]
            size = face.getlength(chunk)
            if used + size > width and chunk != " " and line:
                lines.append(line)
                line, used = [], 0.0
            if chunk == " " and not line:
                continue
            line.append((chunk, bold, link, strike))
            used += size
    lines.append(line)
    return lines


def mock(image_path: Path, caption_path: Path | None, out: Path) -> tuple[int, int]:
    faces = {False: ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), 14),
             True: ImageFont.truetype(str(FONTS / "DejaVuSans-Bold.ttf"), 14)}
    with Image.open(image_path) as post:
        dw, dh = display_size(post.width, post.height)
        shown = post.convert("RGB").resize((dw, dh), Image.Resampling.LANCZOS)
    bubble_w = max(dw, 290)
    markup = caption_path.read_text(encoding="utf-8") if caption_path else ""
    lines = layout(markup, bubble_w - 20, faces) if markup else []
    height = 24 + dh + 12 + len(lines) * 19 + 40
    canvas = Image.new("RGB", (SCREEN, height), "#DCE6D6")
    draw = ImageDraw.Draw(canvas)
    left = 8
    draw.rounded_rectangle((left, 12, left + bubble_w, height - 12), radius=14, fill="white")
    canvas.paste(shown, (left, 12))
    y = 12 + dh + 8
    for line in lines:
        x = left + 10
        for chunk, bold, link, strike in line:
            face = faces[bold]
            color = "#2A7AB8" if link else "#000000"
            draw.text((x, y), chunk, font=face, fill=color)
            width = face.getlength(chunk)
            if strike:
                draw.line((x, y + 9, x + width, y + 9), fill=color, width=1)
            x += width
        y += 19
    canvas.save(out, "JPEG", quality=92)
    return dw, dh


if __name__ == "__main__":
    image, caption, target = sys.argv[1], sys.argv[2] if len(sys.argv) > 3 else None, sys.argv[-1]
    print(mock(Path(image), Path(caption) if caption else None, Path(target)))
