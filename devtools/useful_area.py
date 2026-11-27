"""«Полезное место»: сколько dp² экрана занимает сам товар в посте.

Демо-фото с однородным фоном (термос, робот-пылесос, пароочиститель, берцы) заменяются
масками: пиксели товара — чистый красный, фон прежний. Коллаж версии рендерится как обычно,
затем считаются красные пиксели и переводятся в dp² по масштабу, с которым Telegram
покажет картинку на экране 412 dp (см. shopbot/tools/phone_mock.py).

    python devtools/useful_area.py --bot-dir путь/к/shopbot
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "shopbot" / "tools"


def mask_photo(data: bytes) -> bytes:
    from PIL import Image, ImageChops
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
    w, h = image.size
    corner = image.getpixel((2, 2))
    diff = ImageChops.difference(image, Image.new("RGB", image.size, corner))
    r, g, b = diff.split()
    strongest = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mask = strongest.point(lambda v: 255 if v > 18 else 0)
    out = Image.new("RGB", image.size, corner)
    out.paste((255, 0, 0), (0, 0, w, h), mask)
    buffer = io.BytesIO()
    out.save(buffer, "PNG")
    return buffer.getvalue()


def count_red(jpeg: bytes) -> int:
    from PIL import Image
    with Image.open(io.BytesIO(jpeg)) as image:
        rgb = image.convert("RGB")
    return sum(1 for r, g, b in rgb.getdata() if r > 200 and g < 70 and b < 70)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bot-dir", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.bot_dir.resolve()))
    sys.path.insert(1, str(TOOLS))
    import demo_assets
    import render_examples
    import phone_mock
    import card, collage, drafts  # noqa: E401
    original = demo_assets.jpeg
    uniform = {"thermos", "robot_vacuum", "steam_cleaner", "boots"}
    demo_assets.jpeg = lambda name, quality=90: mask_photo(original(name)) if name in uniform else original(name)
    result = {}
    for count in range(1, 7):
        items = render_examples.make_items(card, drafts, demo_assets, count)
        draft = render_examples.make_draft(drafts, items)
        jpeg = collage.post_photo(draft)
        from PIL import Image
        with Image.open(io.BytesIO(jpeg)) as image:
            w, h = image.size
        dw, dh = phone_mock.display_size(w, h)
        scale = dw / w
        red = count_red(jpeg)
        result[count] = {"product_px": red, "screen_dp2": round(red * scale * scale), "canvas": [w, h],
                         "screen": [dw, dh]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
