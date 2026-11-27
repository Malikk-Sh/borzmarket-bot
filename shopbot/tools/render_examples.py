"""Примеры оформления: карточка и подборки из 2–6 товаров на одних и тех же демо-фото.

Запуск из папки shopbot:
    .venv/bin/python tools/render_examples.py --out examples
Сравнение с другой версией бота (например, прошлой папкой):
    .venv/bin/python tools/render_examples.py --bot-dir ../старая/shopbot --out before

Сохраняет JPEG в фактическом размере, превью шириной 412 px (как экран телефона),
contact sheet и timings.json со временем генерации каждого изображения.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(bot_dir: Path):
    sys.path.insert(0, str(bot_dir))
    sys.path.insert(1, str(HERE))
    os.environ.setdefault("IMAGE_BADGES", "1")
    import card  # noqa: E402
    import collage  # noqa: E402
    import drafts  # noqa: E402
    import demo_assets  # noqa: E402
    return card, collage, drafts, demo_assets


def make_items(card, drafts, demo_assets, count):
    items = []
    for spec in demo_assets.DEMO[:count]:
        product = card.Product(spec["market"], spec["title"], spec["url"], spec.get("price"), spec.get("old_price"))
        if spec.get("conditional_price") is not None:
            product.conditional_price = spec["conditional_price"]
            product.conditional_note = spec["conditional_note"]
        photo = demo_assets.jpeg(spec["photo"]) if spec["photo"] else None
        items.append(drafts.DraftItem(product, photo, 0 if photo else None))
    return tuple(items)


def make_draft(drafts, items, layout="auto"):
    heading = "Подборка BorzMarket" if len(items) > 1 else ""
    fields = drafts.Draft.__dataclass_fields__
    values = dict(id="demo", owner_id=1, chat_id=1, heading=heading, items=items, state="draft", revision=0,
                  view_id=None, due_at=None, destination=None, error="")
    if "layout" in fields:
        values["layout"] = layout
    return drafts.Draft(**values)


def phone_preview(jpeg: bytes, width: int = 412):
    from PIL import Image
    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        scale = width / image.width
        return image.resize((width, round(image.height * scale)), Image.Resampling.LANCZOS)


def contact_sheet(previews, labels, path: Path):
    from PIL import Image, ImageDraw, ImageFont
    pad, label_h = 24, 40
    width = sum(p.width for p in previews) + pad * (len(previews) + 1)
    height = max(p.height for p in previews) + pad * 2 + label_h
    sheet = Image.new("RGB", (width, height), "#20232A")
    draw = ImageDraw.Draw(sheet)
    try:
        face = ImageFont.truetype(str(HERE.parent / "fonts" / "DejaVuSans.ttf"), 22)
    except OSError:
        face = ImageFont.load_default()
    x = pad
    for preview, label in zip(previews, labels):
        draw.text((x, pad // 2), label, font=face, fill="#E8E8E8")
        sheet.paste(preview, (x, pad + label_h - 8))
        x += preview.width + pad
    sheet.save(path, "JPEG", quality=88)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-dir", type=Path, default=HERE.parent)
    parser.add_argument("--out", type=Path, default=HERE.parent / "examples")
    parser.add_argument("--repeat", type=int, default=3, help="повторов для замера времени")
    args = parser.parse_args()
    card, collage, drafts, demo_assets = load(args.bot_dir.resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    timings, previews, labels = {}, [], []
    for count in range(1, 7):
        items = make_items(card, drafts, demo_assets, count)
        draft = make_draft(drafts, items)
        runs = []
        for _ in range(max(1, args.repeat)):
            started = time.perf_counter()
            jpeg = collage.post_photo(draft)
            runs.append(time.perf_counter() - started)
        name = f"post_{count}.jpg"
        (args.out / name).write_bytes(jpeg)
        preview = phone_preview(jpeg)
        preview.save(args.out / f"phone_{count}.jpg", "JPEG", quality=88)
        previews.append(preview)
        from PIL import Image
        with Image.open(io.BytesIO(jpeg)) as image:
            size = image.size
        labels.append(f"{count} · {size[0]}×{size[1]}")
        timings[name] = {"size": size, "bytes": len(jpeg), "median_s": round(statistics.median(runs), 3),
                         "runs_s": [round(r, 3) for r in runs]}
        caption = collage.post_caption(draft)
        (args.out / f"caption_{count}.html").write_text(caption, encoding="utf-8")
    contact_sheet(previews, labels, args.out / "contact_sheet.jpg")
    (args.out / "timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(timings, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
