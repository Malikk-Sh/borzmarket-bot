"""Сравнение оформления «до / после» на одинаковых демо-фото.

    python devtools/visual_compare.py --before reports/before --after reports/after --out reports

Берёт post_N.jpg и caption_N.html из двух папок render_examples.py и собирает:
  compare_contact_sheet.jpg — все размеры 1–6 в том масштабе, в котором их покажет Telegram
                              на экране 412 dp (верхний ряд v9.1, нижний v10);
  feed_N.jpg                — макет ленты канала с подписью для 1, 3 и 6 товаров (слева v9.1);
  visual_metrics.json       — размеры на экране, доля площади, время генерации.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shopbot" / "tools"))
import phone_mock  # noqa: E402

FONT = ROOT / "shopbot" / "fonts" / "DejaVuSans-Bold.ttf"


def label(draw, xy, text, size=18, fill="#EDEDED"):
    draw.text(xy, text, font=ImageFont.truetype(str(FONT), size), fill=fill)


def contact(before: Path, after: Path, out: Path) -> dict:
    rows, metrics = [], {}
    for name, folder in (("v9.1", before), ("v10", after)):
        cells = []
        for n in range(1, 7):
            with Image.open(folder / f"post_{n}.jpg") as image:
                w, h = image.size
                dw, dh = phone_mock.display_size(w, h)
                cells.append(image.convert("RGB").resize((dw, dh), Image.Resampling.LANCZOS))
                metrics.setdefault(f"{n}", {})[name] = {"pixels": [w, h], "screen_dp": [dw, dh],
                                                        "screen_area_dp2": dw * dh}
        rows.append((name, cells))
    pad, head = 16, 30
    width = pad + sum(max(r[1][i].width for r in rows) + pad for i in range(6)) + 60
    height = head + sum(max(c.height for c in cells) + head + pad for _, cells in rows)
    sheet = Image.new("RGB", (width, height), "#1E2126")
    draw = ImageDraw.Draw(sheet)
    y = head
    for name, cells in rows:
        label(draw, (pad, y - 26), f"{name} · как в ленте Telegram на экране 412 dp (1 px = 1 dp)")
        x = pad
        for i, cell in enumerate(cells):
            column = max(r[1][i].width for r in rows)
            label(draw, (x, y), f"{i + 1} · {cell.width}×{cell.height} dp", size=14, fill="#B8BEC6")
            sheet.paste(cell, (x, y + 20))
            x += column + pad
        y += max(c.height for c in cells) + head + pad + 20
    sheet = sheet.crop((0, 0, width, y))
    sheet.save(out, "JPEG", quality=90)
    return metrics


def feeds(before: Path, after: Path, out_dir: Path) -> None:
    for n in (1, 3, 6):
        parts = []
        for folder in (before, after):
            target = out_dir / f"_feed_{folder.name}_{n}.jpg"
            phone_mock.mock(folder / f"post_{n}.jpg", folder / f"caption_{n}.html", target)
            parts.append(Image.open(target))
        gap = 24
        sheet = Image.new("RGB", (sum(p.width for p in parts) + gap * 3, max(p.height for p in parts) + 40), "#1E2126")
        draw = ImageDraw.Draw(sheet)
        x = gap
        for title, part in zip(("v9.1", "v10"), parts):
            label(draw, (x, 10), title)
            sheet.paste(part, (x, 36))
            x += part.width + gap
        sheet.save(out_dir / f"feed_{n}.jpg", "JPEG", quality=90)
        for part in parts:
            part.close()
        for folder in (before, after):
            (out_dir / f"_feed_{folder.name}_{n}.jpg").unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    metrics = contact(args.before, args.after, args.out / "compare_contact_sheet.jpg")
    feeds(args.before, args.after, args.out)
    for name, folder in (("v9.1", args.before), ("v10", args.after)):
        timings = json.loads((folder / "timings.json").read_text(encoding="utf-8"))
        for n in range(1, 7):
            metrics[str(n)][name]["render_s"] = timings[f"post_{n}.jpg"]["median_s"]
            metrics[str(n)][name]["bytes"] = timings[f"post_{n}.jpg"]["bytes"]
    (args.out / "visual_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    for n in range(1, 7):
        b, a = metrics[str(n)]["v9.1"], metrics[str(n)]["v10"]
        print(f"{n}: v9.1 {b['pixels']} → экран {b['screen_dp']} · v10 {a['pixels']} → экран {a['screen_dp']} · "
              f"площадь ×{a['screen_area_dp2'] / b['screen_area_dp2']:.2f} · {b['render_s']} → {a['render_s']} с")


if __name__ == "__main__":
    main()
