"""Режим A эксперимента: установленная версия бота на тех же записанных ответах сети.

Код прежней версии не меняется: её модули подключаются из указанной папки, а запись ответов
и формат CSV берутся из price_lab.py новой версии. Сеть не используется (только воспроизведение).

    python devtools/run_baseline.py ПАПКА_УСТАНОВЛЕННОГО_БОТА ЗАПИСЬ.json --items items.txt --out a.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import os
import sys
import time
from pathlib import Path

NEW = Path(__file__).resolve().parent.parent / "shopbot"


def load_lab():
    spec = importlib.util.spec_from_file_location("price_lab_new", NEW / "price_lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run(args) -> int:
    folder = Path(args.bot).resolve()
    sys.path.insert(0, str(folder))  # import prices, state … — модули установленной версии
    os.environ.update(BHAPI_OZON_TOKEN="", BHAPI_WB_TOKEN="", PRICE_DB="")
    lab = load_lab()
    cassette = lab.Cassette(Path(args.cassette), replay_only=True)
    lab.install_cassette(cassette)
    import prices
    assert Path(prices.__file__).resolve().parent == folder, prices.__file__
    values = [line.strip() for line in Path(args.items).read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")]
    rows = []
    started = time.perf_counter()
    for value in values:
        try:
            product, events, replayed = await lab.run_mode(value, "combo", cassette)
        except Exception as error:  # noqa: BLE001 — строка отчёта вместо падения прогона
            rows += lab.rows_for("baseline", args.env, "A_installed", None, [], False, value,
                                 reason_override=str(error)[:160])
            continue
        rows += lab.rows_for("baseline", args.env, "A_installed", product, events, replayed, value)
    with open(args.out, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=lab.FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    missing = sum(1 for r in rows if r.get("http_status") == "599")
    print(f"Установленная версия из {folder.name}: товаров {len(values)}, строк {len(rows)}, "
          f"ответов из записи {cassette.replayed}, запросов без записи {missing}, "
          f"{time.perf_counter() - started:.1f} с. Файл: {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("bot", help="папка установленной версии (с prices.py)")
    parser.add_argument("cassette", help="запись ответов, сделанная price_lab.py --record")
    parser.add_argument("--items", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--env", default="cloud")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
