"""Собственные демонстрационные фото товаров для примеров оформления и тестов.

Картинки рисуются кодом, поэтому у них нет сторонних прав. Набор нарочно похож на
фото с маркетплейсов: вертикальные кадры 3:4 с белыми полями, горизонтальный кадр,
светло-серый фон и «живой» градиентный фон, который обрезать нельзя.
"""
from __future__ import annotations

import io
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFilter


def _shadow(img: Image.Image, box, blur=22, alpha=70) -> None:
    layer = Image.new("L", img.size, 0)
    ImageDraw.Draw(layer).ellipse(box, fill=alpha)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.paste((0, 0, 0), (0, 0), layer)


def _vgradient(size, top, bottom) -> Image.Image:
    base = Image.new("RGB", size, top)
    mask = Image.linear_gradient("L").resize(size)
    return Image.composite(Image.new("RGB", size, bottom), base, mask)


def _metal(size, dark=(70, 78, 88), light=(226, 230, 235)) -> Image.Image:
    """Горизонтальный «стальной» градиент: тёмный край, блик, тёмный край."""
    w, h = size
    strip = Image.new("RGB", (w, 1))
    for x in range(w):
        t = x / max(1, w - 1)
        k = 1 - abs(t - 0.32) / 0.68
        k = max(0.0, min(1.0, k)) ** 1.6
        strip.putpixel((x, 0), tuple(round(d + (l - d) * k) for d, l in zip(dark, light)))
    return strip.resize((w, h))


def _rounded(img, box, radius, fill=None, source=None):
    x0, y0, x1, y1 = (round(v) for v in box)
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=radius, fill=255)
    if source is None:
        source = Image.new("RGB", mask.size, fill)
    img.paste(source.resize(mask.size), (x0, y0), mask)


def thermos() -> Image.Image:
    """Вертикальный кадр 900×1200, белый фон, товар занимает ~30 % ширины."""
    img = Image.new("RGB", (900, 1200), "white")
    _shadow(img, (300, 1000, 600, 1060))
    _rounded(img, (320, 330, 580, 1030), 46, source=_metal((260, 700)))
    _rounded(img, (335, 250, 565, 380), 30, source=_metal((230, 130), (30, 36, 44), (120, 128, 140)))
    _rounded(img, (320, 395, 580, 425), 8, fill=(40, 46, 54))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((372, 520, 528, 560), radius=18, fill=(30, 110, 90))
    return img


def robot_vacuum() -> Image.Image:
    """Горизонтальный кадр 1200×800, белый фон."""
    img = Image.new("RGB", (1200, 800), "white")
    _shadow(img, (330, 600, 870, 690), blur=26, alpha=80)
    draw = ImageDraw.Draw(img)
    draw.ellipse((340, 130, 860, 650), fill=(38, 40, 46))
    draw.ellipse((370, 160, 830, 620), fill=(58, 62, 70))
    draw.ellipse((420, 210, 780, 570), fill=(30, 32, 36))
    draw.ellipse((548, 250, 652, 354), fill=(200, 205, 212))
    draw.ellipse((566, 268, 634, 336), fill=(90, 96, 104))
    draw.arc((390, 180, 810, 600), 200, 340, fill=(130, 136, 146), width=10)
    draw.rounded_rectangle((560, 470, 640, 492), radius=10, fill=(52, 180, 120))
    return img


def steam_cleaner() -> Image.Image:
    """Вертикальный кадр 900×1200, белый фон, высокий узкий предмет."""
    img = Image.new("RGB", (900, 1200), "white")
    _shadow(img, (260, 1030, 640, 1090))
    draw = ImageDraw.Draw(img)
    _rounded(img, (280, 860, 620, 1060), 60, fill=(236, 240, 244))
    draw.rounded_rectangle((300, 880, 600, 1040), radius=50, outline=(170, 178, 188), width=4)
    _rounded(img, (395, 300, 505, 900), 40, fill=(245, 247, 250))
    draw.rounded_rectangle((395, 300, 505, 900), radius=40, outline=(150, 160, 172), width=4)
    draw.rounded_rectangle((365, 180, 535, 320), radius=36, fill=(225, 55, 70))
    draw.rounded_rectangle((420, 560, 480, 760), radius=24, fill=(60, 130, 220))
    draw.arc((440, 620, 760, 1020), 260, 80, fill=(90, 96, 108), width=16)
    return img


def boots() -> Image.Image:
    """Квадрат 1000×1000, светло-серый фон: пара тёмных берцев."""
    img = Image.new("RGB", (1000, 1000), (242, 242, 242))
    _shadow(img, (170, 760, 860, 840), blur=24, alpha=90)
    draw = ImageDraw.Draw(img)
    for dx, tone in ((0, (52, 44, 36)), (250, (64, 54, 44))):
        x = 200 + dx
        draw.polygon([(x, 260), (x + 170, 260), (x + 180, 600), (x + 330, 650), (x + 350, 780),
                      (x - 10, 780), (x - 10, 640)], fill=tone)
        draw.rectangle((x - 12, 760, x + 352, 800), fill=(24, 22, 20))
        for k in range(7):
            y = 300 + k * 42
            draw.line((x + 40, y, x + 140, y + 12), fill=(200, 190, 170), width=6)
    return img


def backpack() -> Image.Image:
    """Вертикальный «живой» кадр с градиентом: поля обрезать нельзя."""
    img = _vgradient((900, 1200), (214, 196, 170), (150, 126, 100))
    draw = ImageDraw.Draw(img)
    _shadow(img, (170, 1040, 730, 1150), blur=30, alpha=90)
    draw.rounded_rectangle((180, 230, 720, 1100), radius=110, fill=(30, 32, 36))
    draw.rounded_rectangle((250, 640, 650, 1040), radius=70, fill=(44, 46, 52))
    draw.rounded_rectangle((330, 140, 570, 260), radius=60, outline=(30, 32, 36), width=34)
    draw.line((260, 660, 640, 660), fill=(90, 92, 98), width=8)
    draw.rounded_rectangle((420, 420, 480, 470), radius=10, fill=(200, 160, 60))
    return img


def coffee_machine() -> Image.Image:
    """Квадратный кадр 1000×1000, белый фон."""
    img = Image.new("RGB", (1000, 1000), "white")
    _shadow(img, (230, 850, 770, 910))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((260, 160, 740, 880), radius=40, fill=(46, 48, 54))
    draw.rounded_rectangle((300, 200, 700, 380), radius=24, fill=(24, 26, 30))
    draw.rounded_rectangle((420, 420, 580, 470), radius=12, fill=(160, 166, 176))
    draw.rounded_rectangle((440, 600, 560, 760), radius=16, fill=(236, 236, 236))
    draw.rectangle((300, 800, 700, 840), fill=(120, 126, 136))
    return img


PHOTOS = {
    "thermos": thermos,
    "robot_vacuum": robot_vacuum,
    "steam_cleaner": steam_cleaner,
    "boots": boots,
    "backpack": backpack,
    "coffee_machine": coffee_machine,
}


def jpeg(name: str, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    PHOTOS[name]().save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


# Демонстрационный набор. Цены и названия вымышлены, кроме длинного названия пароочистителя
# из ТЗ (кейс короткой ссылки ozon.ru/t/377XHKT) — оно взято как пример длинного текста.
DEMO = [
    dict(market="ozon", photo="thermos", url="https://www.ozon.ru/product/termos-1000000001/",
         title="Термос стальной 1 л с ситечком и чашкой, держит тепло 24 часа",
         price=Decimal("1299"), old_price=Decimal("2590")),
    dict(market="wb", photo="robot_vacuum", url="https://www.wildberries.ru/catalog/100000002/detail.aspx",
         title="Робот-пылесос с влажной уборкой и лидаром, станция самоочистки",
         price=Decimal("41133"), conditional_price=Decimal("39900"), conditional_note="с WB Кошельком"),
    dict(market="ozon", photo="steam_cleaner", url="https://www.ozon.ru/product/paroochistitel-1000000003/",
         title="Пароочиститель для уборки дома мощный / Парогенератор профессиональный 15000 Вт, 15 Бар / 15 насадок",
         price=Decimal("4009"), old_price=Decimal("25000"), conditional_price=Decimal("3608"),
         conditional_note="С банками"),
    dict(market="wb", photo="boots", url="https://www.wildberries.ru/catalog/100000004/detail.aspx",
         title="Берцы мужские тактические зимние, натуральная кожа", price=None),
    dict(market="ozon", photo="backpack", url="https://www.ozon.ru/product/ryukzak-1000000005/",
         title="Рюкзак мужской городской для ноутбука с USB, трансформер, 30 л",
         price=Decimal("2969"), old_price=Decimal("14976")),
    dict(market="wb", photo=None, url="https://www.wildberries.ru/catalog/100000006/detail.aspx",
         title="Кофемашина автоматическая с капучинатором", price=Decimal("129990")),
]
