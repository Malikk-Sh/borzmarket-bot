"""Скачивание и проверка фото товара. Без Telegram: нужен и боту, и `prices.py --diag`.

Ссылка в галерее ещё не значит, что файл доступен: фото считается полученным, только если
файл скачан с разрешённого домена площадки и декодирован в JPEG.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time

import httpx
from PIL import Image, ImageOps

from card import Product
from diagnostics import event, step
from links import IMAGE_DOMAINS, LinkError, get_allowed

log = logging.getLogger("shopbot.photos")

IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def convert_to_jpeg(raw: bytes) -> bytes:
    with Image.open(io.BytesIO(raw)) as source:
        with ImageOps.exif_transpose(source).convert("RGB") as img:
            img.thumbnail((1280, 1280))
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def image_variants(url: str) -> list[str]:
    """Если крупный снимок WB недоступен, пробуем средний размер того же кадра."""
    match = re.fullmatch(r"(https://basket-\d+\.wbbasket\.ru/vol\d+/part\d+/\d+/images/)big(/\d+\.webp)", url)
    return [url, match[1] + "c516x688" + match[2]] if match else [url]


async def download_jpeg(
    product: Product, indexes: tuple[int, ...] | None = None
) -> tuple[bytes | None, int | None]:
    """Скачивает и декодирует первое доступное фото. Ссылка в галерее ещё не значит, что файл есть."""
    if indexes is None:
        indexes = tuple(range(len(product.image_urls)))
    if not indexes:
        return None, None
    proxy = os.getenv(f"{product.marketplace.upper()}_PROXY") or None
    try:
        async with httpx.AsyncClient(headers=IMAGE_HEADERS, timeout=15, proxy=proxy) as client:
            for index in indexes:
                value = product.image_urls[index]
                url = "https:" + value if value.startswith("//") else value
                for variant in image_variants(url):
                    started = time.perf_counter()
                    try:
                        response = await get_allowed(client, variant, IMAGE_DOMAINS[product.marketplace])
                        step(f"Фото {index+1}", f"HTTP {response.status_code}")
                        ms = int((time.perf_counter() - started) * 1000)
                        if response.status_code == 200 and response.content:
                            jpeg = await asyncio.to_thread(convert_to_jpeg, response.content)
                            event("photo", "ok", http=200, ms=ms, detail=f"фото {index + 1}")
                            return jpeg, index
                        event("photo", "error", http=response.status_code, ms=ms, detail=f"фото {index + 1}")
                    except (LinkError, httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError) as error:
                        step(f"Фото {index+1}", type(error).__name__)
                        event("photo", "error", ms=int((time.perf_counter() - started) * 1000),
                              detail=f"фото {index + 1}: {type(error).__name__}")
                        continue
    except (ImportError, ValueError, httpx.HTTPError) as error:
        log.warning("Не удалось открыть соединение для фото (%s)", type(error).__name__)
    return None, None
