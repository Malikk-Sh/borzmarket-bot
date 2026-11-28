"""Время и память загрузки карточки и подборки тем же кодом, что у бота (без Telegram).

    .venv/bin/python ../devtools/measure_collection.py ozon:1526882900
    .venv/bin/python ../devtools/measure_collection.py ozon:1526882900 wb:866768707 … (до шести)

Запускать из папки shopbot. Рабочая база не открывается (память только в процессе), платные
ключи выключены, в канал ничего не отправляется. Печатает время готовой подборки, время каждого
товара по событиям, пик памяти процесса и что получено по каждому полю.
"""
from __future__ import annotations

import asyncio
import os
import resource
import sys
import time
from pathlib import Path

HERE = Path.cwd()
sys.path.insert(0, str(HERE))
os.environ.update(BHAPI_OZON_TOKEN="", BHAPI_WB_TOKEN="")


async def main(values: list[str]) -> int:
    import httpx
    import bot
    import prices
    import state
    state.reset()
    links = []
    for value in values:
        market, url = prices.market_url(value)
        links.append((market, httpx.URL(url)))
    started = time.perf_counter()
    responses = await bot.retrieve_links(links, asyncio.Semaphore(2))
    took = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"Товаров {len(values)} · готово за {took:.1f} с · пик памяти {peak:.0f} МБ · "
          f"режим цен oz={prices.engine('ozon')} wb={prices.engine('wb')}")
    for value, response in zip(values, responses):
        if not isinstance(response, tuple):
            print(f"- {value}: ошибка {type(response).__name__}")
            continue
        item, _ = response
        p = item.product
        ms = sum(e.ms or 0 for e in p.trace)
        origin = {"memory": "память", "aggregator": "резерв"}.get(p.price_origin, "своя")
        price = f"{p.price} ₽ ({p.price_source}, {origin})" if p.price is not None else "нет цены"
        print(f"- {value}: {price} · название {'да' if p.title else 'нет'} · фото {'да' if item.photo else 'нет'}"
              f" · запросы {ms / 1000:.1f} с")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
